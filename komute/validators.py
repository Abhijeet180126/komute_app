import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class PasswordComplexityValidator:
    """Requires at least one uppercase letter, one lowercase letter,
    one digit, and one special character."""

    def validate(self, password, user=None):
        errors = []
        if not re.search(r'[A-Z]', password):
            errors.append(_("an uppercase letter"))
        if not re.search(r'[a-z]', password):
            errors.append(_("a lowercase letter"))
        if not re.search(r'\d', password):
            errors.append(_("a number"))
        if not re.search(r'[^A-Za-z0-9]', password):
            errors.append(_("a special symbol"))

        if errors:
            raise ValidationError(
                _("Password must contain at least %(list)s.")
                % {'list': ', '.join(errors)},
                code='password_missing_complexity',
            )

    def get_help_text(self):
        return _(
            "Your password must contain an uppercase letter, a lowercase "
            "letter, a number, and a special symbol."
        )
