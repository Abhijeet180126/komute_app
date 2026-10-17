from django import template

from komute.models import UserReport

register = template.Library()


@register.inclusion_tag('report-block.html')
def report_block_dialogs():
    """Report and Block dialogs (ISSUES.md A1). Usage, once per page:
    {% load safety_tags %}{% report_block_dialogs %}"""
    return {'reasons': UserReport.REASON_CHOICES}
