# -*- coding: utf-8 -*-
from django.conf import settings

import fudge
import mock
from datetime import datetime, timedelta
from jingo import env
from nose.tools import eq_
from urlparse import urljoin

import amo
import amo.tests
from amo.utils import urlparams
from mkt.site.helpers import absolutify, css, f, js, product_as_dict, timesince
from mkt.site.fixtures import fixture
from mkt.webapps.models import Webapp


class TestCSS(amo.tests.TestCase):

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', True)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_dev_unminified(self, fake_css):
        request = mock.Mock()
        request.GET = {}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_css.expects('css').with_args('mkt/devreg', False, True)
        css(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', False)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_prod_minified(self, fake_css):
        request = mock.Mock()
        request.GET = {}
        context = {'request': request}

        # Should be called with `debug=False`.
        fake_css.expects('css').with_args('mkt/devreg', False, False)
        css(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', True)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_dev_unminified_overridden(self, fake_css):
        request = mock.Mock()
        request.GET = {'debug': 'true'}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_css.expects('css').with_args('mkt/devreg', False, True)
        css(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', False)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_prod_unminified_overridden(self, fake_css):
        request = mock.Mock()
        request.GET = {'debug': 'true'}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_css.expects('css').with_args('mkt/devreg', False, True)
        css(context, 'mkt/devreg')


class TestJS(amo.tests.TestCase):

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', True)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_dev_unminified(self, fake_js):
        request = mock.Mock()
        request.GET = {}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_js.expects('js').with_args('mkt/devreg', True, False, False)
        js(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', False)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_prod_minified(self, fake_js):
        request = mock.Mock()
        request.GET = {}
        context = {'request': request}

        # Should be called with `debug=False`.
        fake_js.expects('js').with_args('mkt/devreg', False, False, False)
        js(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', True)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_dev_unminified_overridden(self, fake_js):
        request = mock.Mock()
        request.GET = {'debug': 'true'}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_js.expects('js').with_args('mkt/devreg', True, False, False)
        js(context, 'mkt/devreg')

    @mock.patch.object(settings, 'TEMPLATE_DEBUG', False)
    @fudge.patch('mkt.site.helpers.jingo_minify_helpers')
    def test_prod_unminified_overridden(self, fake_js):
        request = mock.Mock()
        request.GET = {'debug': 'true'}
        context = {'request': request}

        # Should be called with `debug=True`.
        fake_js.expects('js').with_args('mkt/devreg', True, False, False)
        js(context, 'mkt/devreg')


class TestProductAsDict(amo.tests.TestCase):
    fixtures = fixture('webapp_337141')

    def test_correct(self):
        request = mock.Mock(GET={'src': 'poop'})
        app = Webapp.objects.get(id=337141)

        data = product_as_dict(request, app)
        eq_(data['src'], 'poop')
        eq_(data['is_packaged'], False)
        eq_(data['categories'], [])
        eq_(data['name'], 'Something Something Steamcube!')
        eq_(data['id'], '337141')
        eq_(data['manifest_url'], 'http://micropipes.com/temp/steamcube.webapp')

        tokenUrl = '/reviewers/app/something-something/token'
        recordUrl = '/app/something-something/purchase/record?src=poop'
        assert tokenUrl in data['tokenUrl'], (
            'Invalid Token URL. Expected %s; Got %s'
            % (tokenUrl, data['tokenUrl']))
        assert recordUrl in data['recordUrl'], (
            'Invalid Record URL. Expected %s; Got %s'
            % (recordUrl, data['recordUrl']))


def test_absolutify():
    eq_(absolutify('/woo'), urljoin(settings.SITE_URL, '/woo'))
    eq_(absolutify('https://marketplace.firefox.com'),
        'https://marketplace.firefox.com')


def test_timesince():
    month_ago = datetime.now() - timedelta(days=30)
    eq_(timesince(month_ago), u'1 month ago')
    eq_(timesince(None), u'')


def render(s, context={}):
    return env.from_string(s).render(context)


@mock.patch('mkt.site.helpers.reverse')
def test_url(mock_reverse):
    render('{{ url("viewname", 1, z=2) }}')
    mock_reverse.assert_called_with('viewname', args=(1,), kwargs={'z': 2})

    render('{{ url("viewname", 1, z=2, host="myhost") }}')
    mock_reverse.assert_called_with('viewname', args=(1,), kwargs={'z': 2})


def test_url_src():
    s = render('{{ url("mkt.developers.apps.edit", "a3615", src="xxx") }}')
    assert s.endswith('?src=xxx')


def test_f():
    # This makes sure there's no UnicodeEncodeError when doing the string
    # interpolation.
    eq_(render(u'{{ "foo {0}"|f("baré") }}'), u'foo baré')


def test_isotime():
    time = datetime(2009, 12, 25, 10, 11, 12)
    s = render('{{ d|isotime }}', {'d': time})
    eq_(s, '2009-12-25T18:11:12Z')
    s = render('{{ d|isotime }}', {'d': None})
    eq_(s, '')


def test_urlparams():
    url = '/developers'
    c = {'base': url,
         'base_frag': url + '#hash',
         'base_query': url + '?x=y',
         'sort': 'name', 'frag': 'frag'}

    # Adding a query.
    s = render('{{ base_frag|urlparams(sort=sort) }}', c)
    eq_(s, '%s?sort=name#hash' % url)

    # Adding a fragment.
    s = render('{{ base|urlparams(frag) }}', c)
    eq_(s, '%s#frag' % url)

    # Replacing a fragment.
    s = render('{{ base_frag|urlparams(frag) }}', c)
    eq_(s, '%s#frag' % url)

    # Adding query and fragment.
    s = render('{{ base_frag|urlparams(frag, sort=sort) }}', c)
    eq_(s, '%s?sort=name#frag' % url)

    # Adding query with existing params.
    s = render('{{ base_query|urlparams(frag, sort=sort) }}', c)
    eq_(s, '%s?sort=name&amp;x=y#frag' % url)

    # Replacing a query param.
    s = render('{{ base_query|urlparams(frag, x="z") }}', c)
    eq_(s, '%s?x=z#frag' % url)

    # Params with value of None get dropped.
    s = render('{{ base|urlparams(sort=None) }}', c)
    eq_(s, url)

    # Removing a query
    s = render('{{ base_query|urlparams(x=None) }}', c)
    eq_(s, url)


def test_urlparams_unicode():
    url = u'/xx?evil=reco\ufffd\ufffd\ufffd\u02f5'
    urlparams(url)
