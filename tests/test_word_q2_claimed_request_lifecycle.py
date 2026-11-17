import json
import os
import threading
import time

import pytest

from providers.base import ProviderError
from providers.qoder_internal import QoderInternalProvider


REPLY = "<<<CU_START:CU-01>>>\n按原始记录核对并登记处理结果。\n<<<CU_END>>>\n"


def provider_and_request(tmp_path):
    root = tmp_path / "queue"
    for folder in ["out", "in", "claims"]:
        (root / folder).mkdir(parents=True)
    provider = QoderInternalProvider({"provider_name": "qoder_internal", "queue_dir": str(root), "wait_timeout": 0.4, "poll_interval": 0.01, "test_mode": True})
    request = {"job_id": "OWNED-01", "task_id": "OWNED-01", "response_protocol": "cu_envelope", "required_keys": ["cu_envelope_text"], "prompt": "offline lifecycle fixture"}
    return provider, request, root


def claim_worker(root, answer=False):
    errors = []
    def work():
        try:
            deadline = time.monotonic() + 5
            source = root / "out" / "OWNED-01.request.json"
            while not source.is_file():
                if time.monotonic() >= deadline:
                    raise AssertionError("fixture request did not arrive")
                time.sleep(0.005)
            os.replace(source, root / "claims" / source.name)
            if answer:
                (root / "in" / "OWNED-01.response.json").write_text(json.dumps({"job_id": "OWNED-01", "status": "OK", "body": REPLY}), encoding="utf-8")
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, errors


def test_claimed_timeout_can_recover_one_late_answer_without_another_execution(tmp_path):
    provider, request, root = provider_and_request(tmp_path)
    thread, errors = claim_worker(root)
    with pytest.raises(ProviderError) as caught:
        provider.invoke_structured(request, tmp_path / "first")
    thread.join(5)
    assert not errors and caught.value.error_code == "TIMEOUT"
    assert (root / "abandoned" / "OWNED-01.TIMEOUT.request.json").is_file()
    assert not (root / "claims" / "OWNED-01.request.json").exists()
    (root / "in" / "OWNED-01.response.json").write_text(json.dumps({"job_id": "OWNED-01", "status": "OK", "body": REPLY}), encoding="utf-8")
    recovered = provider.collect_late_response(request, tmp_path / "late")
    assert recovered[1]["late_response_recovered"] is True
    assert (root / "done" / "OWNED-01.request.json").is_file()
    assert provider.collect_late_response(request, tmp_path / "again") is None
    events = [json.loads(line)["event"] for line in (root / "ledger" / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events.count("REQUEST_ENQUEUED") == 1
    assert events.count("LATE_RESPONSE_RECOVERED") == 1


def test_successful_claimed_request_is_retired(tmp_path):
    provider, request, root = provider_and_request(tmp_path)
    thread, errors = claim_worker(root, answer=True)
    result = provider.invoke_structured(request, tmp_path / "success")
    thread.join(5)
    assert not errors and "cu_envelope_text" in result
    assert (root / "done" / "OWNED-01.request.json").is_file()
    assert not (root / "claims" / "OWNED-01.request.json").exists()


def test_timeout_never_moves_another_jobs_claim(tmp_path):
    provider, request, root = provider_and_request(tmp_path)
    other = root / "claims" / "FOREIGN.request.json"
    other.write_text('{"job_id":"FOREIGN"}', encoding="utf-8")
    before = other.read_bytes()
    with pytest.raises(ProviderError):
        provider.invoke_structured(request, tmp_path / "unclaimed")
    assert other.read_bytes() == before
