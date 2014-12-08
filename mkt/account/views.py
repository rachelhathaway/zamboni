import datetime
import hashlib
import hmac
import json
import uuid
import urlparse

from django import http
from django.conf import settings
from django.core.signing import BadSignature, Signer
from django.contrib import auth
from django.contrib.auth.signals import user_logged_in
from django.utils.datastructures import MultiValueDictKeyError

import basket
import commonware.log
from django_browserid import get_audience
from django_statsd.clients import statsd

from requests_oauthlib import OAuth2Session
from rest_framework import status
from rest_framework.decorators import (authentication_classes,
                                       permission_classes)
from rest_framework.exceptions import AuthenticationFailed, ParseError
from rest_framework.generics import (CreateAPIView, DestroyAPIView,
                                     RetrieveAPIView, RetrieveUpdateAPIView,
                                     UpdateAPIView)
from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.viewsets import GenericViewSet

import amo
from amo.utils import log_cef
from lib.metrics import record_action
from mkt.users.models import UserProfile
from mkt.users.tasks import send_fxa_mail
from mkt.users.views import browserid_authenticate

from mkt.account.serializers import (AccountSerializer, AccountInfoSerializer,
                                     FeedbackSerializer, FxALoginSerializer,
                                     LoginSerializer, NewsletterSerializer,
                                     PermissionsSerializer)
from mkt.account.utils import PREVERIFY_KEY, fxa_preverify_token
from mkt.api.authentication import (RestAnonymousAuthentication,
                                    RestOAuthAuthentication,
                                    RestSharedSecretAuthentication)
from mkt.api.authorization import AllowSelf, AllowOwner
from mkt.api.base import CORSMixin, MarketplaceView, cors_api_view
from mkt.constants.apps import INSTALL_TYPE_USER
from mkt.site.mail import send_mail_jinja
from mkt.webapps.serializers import SimpleAppSerializer
from mkt.webapps.models import Installed, Webapp


log = commonware.log.getLogger('z.account')


def user_relevant_apps(user):
    return {
        'developed': list(user.addonuser_set.filter(
            role=amo.AUTHOR_ROLE_OWNER).values_list('addon_id', flat=True)),
        'installed': list(user.installed_set.values_list('addon_id',
            flat=True)),
        'purchased': list(user.purchase_ids()),
    }


class MineMixin(object):
    def get_object(self, queryset=None):
        pk = self.kwargs.get('pk')
        if pk == 'mine':
            self.kwargs['pk'] = self.request.user.pk
        return super(MineMixin, self).get_object(queryset)


class InstalledViewSet(CORSMixin, MarketplaceView, ListModelMixin,
                       GenericViewSet):
    cors_allowed_methods = ['get']
    serializer_class = SimpleAppSerializer
    permission_classes = [AllowSelf]
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]

    def get_queryset(self):
        return Webapp.objects.no_cache().filter(
            installed__user=self.request.user,
            installed__install_type=INSTALL_TYPE_USER).order_by(
                '-installed__created')

    def remove_app(self, request, **kwargs):
        self.cors_allowed_methods = ['post']
        try:
            to_remove = Webapp.objects.get(pk=request.DATA['app'])
        except (KeyError, MultiValueDictKeyError):
            raise ParseError(detail='`app` was not provided.')
        except Webapp.DoesNotExist:
            raise ParseError(detail='`app` does not exist.')
        try:
            installed = request.user.installed_set.get(
                install_type=INSTALL_TYPE_USER, addon_id=to_remove.pk)
            installed.delete()
        except Installed.DoesNotExist:
            raise ParseError(detail='`app` is not installed or not removable.')
        return Response(status=status.HTTP_202_ACCEPTED)


class CreateAPIViewWithoutModel(MarketplaceView, CreateAPIView):
    """
    A base class for APIs that need to support a create-like action, but
    without being tied to a Django Model.
    """
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication,
                              RestAnonymousAuthentication]
    cors_allowed_methods = ['post']
    permission_classes = (AllowAny,)

    def response_success(self, request, serializer, data=None):
        if data is None:
            data = serializer.data
        return Response(data, status=status.HTTP_201_CREATED)

    def response_error(self, request, serializer):
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA)
        if serializer.is_valid():
            data = self.create_action(request, serializer)
            return self.response_success(request, serializer, data=data)
        return self.response_error(request, serializer)


class AccountView(MineMixin, CORSMixin, RetrieveUpdateAPIView):
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]
    cors_allowed_methods = ['get', 'patch', 'put']
    model = UserProfile
    permission_classes = (AllowOwner,)
    serializer_class = AccountSerializer


class AnonymousUserMixin(object):
    def get_object(self, *args, **kwargs):
        try:
            user = super(AnonymousUserMixin, self).get_object(*args, **kwargs)
        except http.Http404:
            # The base get_object() will raise Http404 instead of DoesNotExist.
            # Treat no object as an anonymous user (source: unknown).
            user = UserProfile(is_verified=False)
        return user


class AccountInfoView(AnonymousUserMixin, CORSMixin, RetrieveAPIView):
    permission_classes = []
    cors_allowed_methods = ['get']
    # Only select users with an FxA source, everything else will be unkown.
    queryset = UserProfile.objects.filter(
        last_login_attempt__gt=datetime.datetime(2014, 4, 30))
    serializer_class = AccountInfoSerializer
    lookup_field = 'email'


class ConfirmFxAVerificationView(AnonymousUserMixin, UpdateAPIView):
    permission_classes = []
    authentication_classes = []
    queryset = UserProfile.objects.filter(is_verified=True)
    lookup_field = 'email'

    def update(self, request, *args, **kwargs):
        user = self.get_object()
        if user.is_verified:
            send_fxa_mail.delay([user.pk], 'customers-during', True)
            return Response({'notified': True})
        else:
            return Response({'notified': False})


class FeedbackView(CORSMixin, CreateAPIViewWithoutModel):
    class FeedbackThrottle(UserRateThrottle):
        THROTTLE_RATES = {
            'user': '30/hour',
        }

    serializer_class = FeedbackSerializer
    throttle_classes = (FeedbackThrottle,)
    throttle_scope = 'user'

    def create_action(self, request, serializer):
        context_data = self.get_context_data(request, serializer)
        sender = getattr(request.user, 'email', settings.NOBODY_EMAIL)
        send_mail_jinja(u'Marketplace Feedback', 'account/email/feedback.txt',
                        context_data, from_email=sender,
                        recipient_list=[settings.MKT_FEEDBACK_EMAIL])

    def get_context_data(self, request, serializer):
        context_data = {
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            'ip_address': request.META.get('REMOTE_ADDR', '')
        }
        context_data.update(serializer.data)
        context_data['user'] = request.user
        return context_data


def commonplace_token(email):
    unique_id = uuid.uuid4().hex

    consumer_id = hashlib.sha1(
        email + settings.SECRET_KEY).hexdigest()

    hm = hmac.new(
        unique_id + settings.SECRET_KEY,
        consumer_id, hashlib.sha512)

    return ','.join((email, hm.hexdigest(), unique_id))


def fxa_oauth_api(name):
    return urlparse.urljoin(settings.FXA_OAUTH_URL, 'v1/' + name)


def find_or_create_user(email, fxa_uid, userid):

    def find_user(**kwargs):
        try:
            return UserProfile.objects.get(**kwargs)
        except UserProfile.DoesNotExist:
            return None

    profile = (find_user(pk=userid) or find_user(username=fxa_uid)
               or find_user(email=email))
    if profile:
        created = False
        profile.update(username=fxa_uid, email=email)
    else:
        created = True
        profile = UserProfile.objects.create(
            username=fxa_uid,
            email=email,
            source=amo.LOGIN_SOURCE_FXA,
            display_name=email.partition('@')[0],
            is_verified=True)

    if profile.source != amo.LOGIN_SOURCE_FXA:
        log.info('Set account to FxA for {0}'.format(email))
        statsd.incr('z.mkt.user.fxa')
        profile.update(source=amo.LOGIN_SOURCE_FXA)

    return profile, created


def fxa_authorize(session, client_secret, auth_response):
    token = session.fetch_token(
        fxa_oauth_api('token'),
        authorization_response=auth_response,
        client_secret=client_secret)
    res = session.post(
        fxa_oauth_api('verify'),
        data=json.dumps({'token': token['access_token']}),
        headers={'Content-Type': 'application/json'})
    return res.json()


class FxALoginView(CORSMixin, CreateAPIViewWithoutModel):
    authentication_classes = []
    serializer_class = FxALoginSerializer

    def create_action(self, request, serializer):
        client_id = request.POST.get('client_id', settings.FXA_CLIENT_ID)
        secret = settings.FXA_SECRETS[client_id]
        session = OAuth2Session(
            client_id,
            scope=u'profile',
            state=serializer.data['state'])

        try:
            # Maybe this was a preverified login to migrate a user.
            userid = Signer().unsign(serializer.data['state'])
        except BadSignature:
            userid = None

        auth_response = serializer.data['auth_response']
        fxa_authorization = fxa_authorize(session, secret, auth_response)

        if 'user' in fxa_authorization:
            email = fxa_authorization['email']
            fxa_uid = fxa_authorization['user']
            profile, created = find_or_create_user(email, fxa_uid, userid)
            if created:
                log_cef('New Account', 5, request, username=fxa_uid,
                        signature='AUTHNOTICE',
                        msg='User created a new account (from FxA)')
                record_action('new-user', request)
            auth.login(request, profile)
            profile.log_login_attempt(True)

            auth.signals.user_logged_in.send(sender=profile.__class__,
                                             request=request,
                                             user=profile)
        else:
            raise AuthenticationFailed('No profile.')

        request.user = profile
        request.groups = profile.groups.all()
        # Remember whether the user has logged in to highlight the register or
        # sign in nav button. 31536000 == one year.
        request.set_cookie('has_logged_in', '1', max_age=5 * 31536000)

        # We want to return completely custom data, not the serializer's.
        data = {
            'error': None,
            'token': commonplace_token(request.user.email),
            'settings': {
                'display_name': request.user.display_name,
                'email': request.user.email,
                'enable_recommendations': request.user.enable_recommendations,
                'source': 'firefox-accounts',
            }
        }
        # Serializers give up if they aren't passed an instance, so we
        # do that here despite PermissionsSerializer not needing one
        # really.
        permissions = PermissionsSerializer(context={'request': request},
                                            instance=True)
        data.update(permissions.data)

        # Add ids of installed/purchased/developed apps.
        data['apps'] = user_relevant_apps(profile)

        return data


@cors_api_view(['POST'])
@authentication_classes([RestOAuthAuthentication,
                         RestSharedSecretAuthentication])
@permission_classes([IsAuthenticated])
def fxa_preverify_view(request):
    if not request.user.is_verified:
        return Response("User's email is not verified", status=403)

    return http.HttpResponse(
        fxa_preverify_token(request.user, datetime.timedelta(minutes=10)),
        content_type='application/jwt')


def fxa_preverify_key(request):
    return http.HttpResponse(
        json.dumps({'keys': [PREVERIFY_KEY.to_dict()]}),
        content_type='application/jwk-set+json')


class LoginView(CORSMixin, CreateAPIViewWithoutModel):
    authentication_classes = []
    serializer_class = LoginSerializer

    def create_action(self, request, serializer):
        with statsd.timer('auth.browserid.verify'):
            profile, msg = browserid_authenticate(
                request, serializer.data['assertion'],
                browserid_audience=serializer.data['audience'] or
                                   get_audience(request),
                is_mobile=serializer.data['is_mobile'],
            )
        if profile is None:
            # Authentication failure.
            log.info('No profile: %s' % (msg or ''))
            raise AuthenticationFailed('No profile.')

        request.user = profile
        request.groups = profile.groups.all()

        auth.login(request, profile)
        profile.log_login_attempt(True)  # TODO: move this to the signal.
        user_logged_in.send(sender=profile.__class__, request=request,
                            user=profile)

        # We want to return completely custom data, not the serializer's.
        data = {
            'error': None,
            'token': commonplace_token(request.user.email),
            'settings': {
                'display_name': request.user.display_name,
                'email': request.user.email,
                'enable_recommendations': request.user.enable_recommendations,
            }
        }
        # Serializers give up if they aren't passed an instance, so we
        # do that here despite PermissionsSerializer not needing one
        # really.
        permissions = PermissionsSerializer(context={'request': request},
                                            instance=True)
        data.update(permissions.data)

        # Add ids of installed/purchased/developed apps.
        data['apps'] = user_relevant_apps(profile)

        return data


class LogoutView(CORSMixin, DestroyAPIView):
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]
    permission_classes = (IsAuthenticated,)
    cors_allowed_methods = ['delete']

    def delete(self, request):
        auth.logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NewsletterView(CORSMixin, CreateAPIViewWithoutModel):
    class NewsletterThrottle(UserRateThrottle):
        scope = 'newsletter'
        THROTTLE_RATES = {
            'newsletter': '30/hour',
        }

    serializer_class = NewsletterSerializer
    throttle_classes = (NewsletterThrottle,)

    def response_success(self, request, serializer, data=None):
        return Response({}, status=status.HTTP_204_NO_CONTENT)

    def create_action(self, request, serializer):
        email = serializer.data['email']
        newsletter = serializer.data['newsletter']
        basket.subscribe(email, newsletter,
                         format='H', country=request.REGION.slug,
                         lang=request.LANG, optin='Y',
                         trigger_welcome='Y')


class PermissionsView(CORSMixin, MineMixin, RetrieveAPIView):

    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]
    cors_allowed_methods = ['get']
    permission_classes = (AllowSelf,)
    model = UserProfile
    serializer_class = PermissionsSerializer
