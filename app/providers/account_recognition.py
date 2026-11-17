"""Bounded whole-document recognition parsing for the two account routes only."""
from .base import ProviderError
from .zcode_recognition import parse_recognition as _parse_recognition


def parse_recognition(provider, request, value):
    if provider not in {'qoder_account', 'zcode_cn'}:
        raise ProviderError('此格式整理仅用于账号识别通道。', error_code='ACCOUNT_PROVIDER_SCOPE')
    try:
        result, audit = _parse_recognition(request, value)
    except ProviderError as exc:
        if provider == 'zcode_cn':
            raise
        raise ProviderError(str(exc).replace('ZCode', 'Qoder'),
                            error_code=exc.error_code.replace('ZCODE_', 'QODER_', 1)) from exc
    if provider == 'qoder_account':
        audit = {**audit, 'type': 'qoder_recognition_serialization'}
    return result, audit
