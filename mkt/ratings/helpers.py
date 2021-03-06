import jingo
import jinja2
from tower import ugettext as _

from mkt.access import acl


@jingo.register.filter
def stars(num, large=False):
    # check for 0.0 incase None was cast to a float. Should
    # be safe since lowest rating you can give is 1.0
    if num is None or num == 0.0:
        return _('Not yet reviewed')
    else:
        num = min(5, int(round(num)))
        t = jingo.env.get_template('ratings/reviews_rating.html')
        # These are getting renamed for contextual sense in the template.
        return jinja2.Markup(t.render({'rating': num, 'detailpage': large}))


@jingo.register.function
def impala_reviews_link(addon):
    t = jingo.env.get_template('ratings/reviews_link.html')
    return jinja2.Markup(t.render({'addon': addon}))


def user_can_delete_review(request, review):
    """Return whether or not the request.user can delete reviews.

    People who can delete reviews:
      * The original review author.
      * Editors, but only if they aren't listed as an author of the add-on.
      * Users in a group with "Users:Edit" privileges.
      * Users in a group with "Addons:Edit" privileges.

    TODO: Make this more granular when we have multiple reviewer types, e.g.
    persona reviewers shouldn't be able to delete add-on reviews.
    """
    is_editor = acl.check_reviewer(request)
    is_author = review.addon.has_author(request.user)
    return (
        review.user_id == request.user.id or
        not is_author and (
            is_editor or
            acl.action_allowed(request, 'Users', 'Edit') or
            acl.action_allowed(request, 'Apps', 'Edit')))
