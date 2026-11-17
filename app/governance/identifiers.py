"""Identity tokens protected from prose cleanup; preservation is not approval."""
import re

EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
EMAIL_LIKE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]*@[A-Za-z0-9.-]+")

def preserve_email_substitution(text, transform):
    parts = []
    start = 0
    for match in EMAIL.finditer(text):
        parts.extend((transform(text[start:match.start()]), match.group(0)))
        start = match.end()
    parts.append(transform(text[start:]))
    return ''.join(parts)

def malformed_email_findings(text, path):
    return [{'rule_id': 'EMAIL_IDENTITY_CORRUPTION', 'path': path, 'severity': 'BLOCK',
             'issue': 'email address is malformed', 'token': match.group(0)}
            for match in EMAIL_LIKE.finditer(text) if not EMAIL.fullmatch(match.group(0))]
