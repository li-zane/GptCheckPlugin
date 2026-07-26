"""Local feasibility probe for a sidecar-to-control-plane credential envelope.

This is a design experiment, not production protocol code. It intentionally
uses synthetic credentials and does not connect to sub2api or a real database.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import statistics
import sys
import time
import tracemalloc
import unittest
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


PROTOCOL_VERSION = 1
MAX_ACCOUNT_IDS = 200
MAX_CIPHERTEXT_BYTES = 1_048_576
MAX_CLOCK_SKEW_SECONDS = 60
ALLOWED_OPERATIONS = {
    "inspect_oauth_metadata",
    "read_api_keys",
}


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _raw_public_key(key: x25519.X25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _derive_key(shared_secret: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"gptcheck-sidecar-envelope-v1",
    ).derive(shared_secret)


def _validate_account_ids(account_ids: Any) -> list[int]:
    if not isinstance(account_ids, list) or not account_ids:
        raise ValueError("account_ids must be a non-empty list")
    if len(account_ids) > MAX_ACCOUNT_IDS:
        raise ValueError("account_ids exceeds the batch limit")
    if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in account_ids):
        raise ValueError("account_ids must contain positive integers")
    if len(set(account_ids)) != len(account_ids):
        raise ValueError("account_ids must not contain duplicates")
    return account_ids


def _aad(envelope: dict[str, Any]) -> bytes:
    return _canonical_json(
        {
            "v": envelope["v"],
            "agent_id": envelope["agent_id"],
            "instance_id": envelope["instance_id"],
            "request_id": envelope["request_id"],
            "operation": envelope["operation"],
            "account_ids": envelope["account_ids"],
            "issued_at": envelope["issued_at"],
            "expires_at": envelope["expires_at"],
            "sequence": envelope["sequence"],
            "key_id": envelope["key_id"],
            "ephemeral_public_key": envelope["ephemeral_public_key"],
            "salt": envelope["salt"],
            "nonce": envelope["nonce"],
        }
    )


def _signed_bytes(envelope: dict[str, Any]) -> bytes:
    return _canonical_json({key: value for key, value in envelope.items() if key != "signature"})


def seal_response(
    *,
    signing_key: ed25519.Ed25519PrivateKey,
    receiver_public_key: x25519.X25519PublicKey,
    agent_id: str,
    instance_id: str,
    request_id: str,
    operation: str,
    account_ids: list[int],
    sequence: int,
    payload: dict[str, Any],
    issued_at: int | None = None,
    ttl_seconds: int = 30,
    key_id: str = "central-k1",
) -> dict[str, Any]:
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("operation is not allowed")
    checked_ids = _validate_account_ids(account_ids)
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if ttl_seconds <= 0 or ttl_seconds > MAX_CLOCK_SKEW_SECONDS:
        raise ValueError("ttl_seconds is outside the allowed range")

    now = int(time.time()) if issued_at is None else issued_at
    ephemeral_key = x25519.X25519PrivateKey.generate()
    salt = os.urandom(32)
    nonce = os.urandom(12)
    ephemeral_public_key = _raw_public_key(ephemeral_key.public_key())
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "agent_id": agent_id,
        "instance_id": instance_id,
        "request_id": request_id,
        "operation": operation,
        "account_ids": checked_ids,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
        "sequence": sequence,
        "key_id": key_id,
        "ephemeral_public_key": _b64_encode(ephemeral_public_key),
        "salt": _b64_encode(salt),
        "nonce": _b64_encode(nonce),
    }
    key = _derive_key(ephemeral_key.exchange(receiver_public_key), salt)
    plaintext = _canonical_json(payload)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, _aad(envelope))
    if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
        raise ValueError("ciphertext exceeds the response limit")
    envelope["ciphertext"] = _b64_encode(ciphertext)
    envelope["signature"] = _b64_encode(signing_key.sign(_signed_bytes(envelope)))
    return envelope


@dataclass
class ReplayGuard:
    last_sequence: int = 0
    request_ids: set[str] = field(default_factory=set)

    def check(self, request_id: str, sequence: int) -> None:
        if request_id in self.request_ids:
            raise ValueError("request_id was replayed")
        if sequence <= self.last_sequence:
            raise ValueError("sequence was replayed or arrived out of order")

    def commit(self, request_id: str, sequence: int) -> None:
        self.request_ids.add(request_id)
        self.last_sequence = sequence


def open_response(
    envelope: dict[str, Any],
    *,
    signing_public_key: ed25519.Ed25519PublicKey,
    receiver_private_key: x25519.X25519PrivateKey,
    replay_guard: ReplayGuard,
    expected_agent_id: str,
    expected_instance_id: str,
    expected_operation: str,
    now: int | None = None,
) -> dict[str, Any]:
    required_fields = {
        "v",
        "agent_id",
        "instance_id",
        "request_id",
        "operation",
        "account_ids",
        "issued_at",
        "expires_at",
        "sequence",
        "key_id",
        "ephemeral_public_key",
        "salt",
        "nonce",
        "ciphertext",
        "signature",
    }
    if set(envelope) != required_fields:
        raise ValueError("envelope fields do not match the protocol schema")
    if envelope["v"] != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if envelope["operation"] not in ALLOWED_OPERATIONS:
        raise ValueError("operation is not allowed")
    if envelope["agent_id"] != expected_agent_id:
        raise ValueError("agent_id does not match the authenticated peer")
    if envelope["instance_id"] != expected_instance_id:
        raise ValueError("instance_id does not match the request scope")
    if envelope["operation"] != expected_operation:
        raise ValueError("operation does not match the request")
    _validate_account_ids(envelope["account_ids"])

    ciphertext = _b64_decode(envelope["ciphertext"])
    if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
        raise ValueError("ciphertext exceeds the response limit")
    signing_public_key.verify(_b64_decode(envelope["signature"]), _signed_bytes(envelope))

    checked_at = int(time.time()) if now is None else now
    if envelope["issued_at"] > checked_at + MAX_CLOCK_SKEW_SECONDS:
        raise ValueError("message was issued too far in the future")
    if envelope["expires_at"] < checked_at:
        raise ValueError("message has expired")
    if envelope["expires_at"] - envelope["issued_at"] > MAX_CLOCK_SKEW_SECONDS:
        raise ValueError("message lifetime exceeds the protocol limit")
    replay_guard.check(envelope["request_id"], envelope["sequence"])

    ephemeral_public_key = x25519.X25519PublicKey.from_public_bytes(
        _b64_decode(envelope["ephemeral_public_key"])
    )
    salt = _b64_decode(envelope["salt"])
    nonce = _b64_decode(envelope["nonce"])
    key = _derive_key(receiver_private_key.exchange(ephemeral_public_key), salt)
    plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, _aad(envelope))
    payload = json.loads(plaintext.decode("utf-8"))
    replay_guard.commit(envelope["request_id"], envelope["sequence"])
    return payload


class ProtocolProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.central_key = x25519.X25519PrivateKey.generate()
        self.agent_key = ed25519.Ed25519PrivateKey.generate()
        self.guard = ReplayGuard()

    def envelope(self, *, sequence: int = 1, issued_at: int | None = None) -> dict[str, Any]:
        return seal_response(
            signing_key=self.agent_key,
            receiver_public_key=self.central_key.public_key(),
            agent_id="agent-a",
            instance_id="instance-a",
            request_id=str(uuid.uuid4()),
            operation="inspect_oauth_metadata",
            account_ids=[7],
            sequence=sequence,
            issued_at=issued_at,
            payload={"accounts": [{"account_id": 7, "plan": "team", "rt_present": True}]},
        )

    def open(self, envelope: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        return open_response(
            envelope,
            signing_public_key=self.agent_key.public_key(),
            receiver_private_key=self.central_key,
            replay_guard=self.guard,
            expected_agent_id="agent-a",
            expected_instance_id="instance-a",
            expected_operation="inspect_oauth_metadata",
            now=now,
        )

    def test_round_trip(self) -> None:
        self.assertEqual(self.open(self.envelope())["accounts"][0]["account_id"], 7)

    def test_ciphertext_tampering_is_rejected(self) -> None:
        envelope = self.envelope()
        ciphertext = bytearray(_b64_decode(envelope["ciphertext"]))
        ciphertext[-1] ^= 1
        envelope["ciphertext"] = _b64_encode(bytes(ciphertext))
        with self.assertRaises(InvalidSignature):
            self.open(envelope)

    def test_authenticated_metadata_tampering_is_rejected(self) -> None:
        envelope = self.envelope()
        envelope["account_ids"] = [8]
        with self.assertRaises(InvalidSignature):
            self.open(envelope)

    def test_replay_is_rejected(self) -> None:
        envelope = self.envelope()
        self.open(envelope)
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.open(envelope)

    def test_expired_message_is_rejected(self) -> None:
        now = int(time.time())
        with self.assertRaisesRegex(ValueError, "expired"):
            self.open(self.envelope(issued_at=now - 120), now=now)

    def test_wrong_receiver_cannot_decrypt(self) -> None:
        envelope = self.envelope()
        with self.assertRaises(InvalidTag):
            open_response(
                envelope,
                signing_public_key=self.agent_key.public_key(),
                receiver_private_key=x25519.X25519PrivateKey.generate(),
                replay_guard=ReplayGuard(),
                expected_agent_id="agent-a",
                expected_instance_id="instance-a",
                expected_operation="inspect_oauth_metadata",
            )

    def test_cross_instance_response_is_rejected(self) -> None:
        envelope = self.envelope()
        with self.assertRaisesRegex(ValueError, "instance_id"):
            open_response(
                envelope,
                signing_public_key=self.agent_key.public_key(),
                receiver_private_key=self.central_key,
                replay_guard=ReplayGuard(),
                expected_agent_id="agent-a",
                expected_instance_id="instance-b",
                expected_operation="inspect_oauth_metadata",
            )

    def test_oversized_batch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "batch limit"):
            seal_response(
                signing_key=self.agent_key,
                receiver_public_key=self.central_key.public_key(),
                agent_id="agent-a",
                instance_id="instance-a",
                request_id=str(uuid.uuid4()),
                operation="read_api_keys",
                account_ids=list(range(1, MAX_ACCOUNT_IDS + 2)),
                sequence=1,
                payload={"accounts": []},
            )


def _process_memory() -> dict[str, int | None]:
    if sys.platform == "win32":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if ok:
            return {
                "rss_bytes": int(counters.WorkingSetSize),
                "peak_rss_bytes": int(counters.PeakWorkingSetSize),
            }
    return {"rss_bytes": None, "peak_rss_bytes": None}


def run_benchmark(iterations: int = 500, accounts_per_envelope: int = 100) -> dict[str, Any]:
    central_key = x25519.X25519PrivateKey.generate()
    agent_key = ed25519.Ed25519PrivateKey.generate()
    guard = ReplayGuard()
    account_ids = list(range(1, accounts_per_envelope + 1))
    synthetic_payload = {
        "accounts": [
            {
                "account_id": account_id,
                "api_key": f"synthetic-{account_id:04d}-" + "x" * 48,
            }
            for account_id in account_ids
        ]
    }
    latencies_ms: list[float] = []
    encoded_sizes: list[int] = []
    memory_before = _process_memory()
    tracemalloc.start()
    started_at = time.perf_counter()
    for sequence in range(1, iterations + 1):
        item_started_at = time.perf_counter()
        envelope = seal_response(
            signing_key=agent_key,
            receiver_public_key=central_key.public_key(),
            agent_id="benchmark-agent",
            instance_id="benchmark-instance",
            request_id=str(uuid.uuid4()),
            operation="read_api_keys",
            account_ids=account_ids,
            sequence=sequence,
            payload=synthetic_payload,
        )
        opened = open_response(
            envelope,
            signing_public_key=agent_key.public_key(),
            receiver_private_key=central_key,
            replay_guard=guard,
            expected_agent_id="benchmark-agent",
            expected_instance_id="benchmark-instance",
            expected_operation="read_api_keys",
        )
        if len(opened["accounts"]) != accounts_per_envelope:
            raise RuntimeError("benchmark round trip lost account records")
        latencies_ms.append((time.perf_counter() - item_started_at) * 1000)
        encoded_sizes.append(len(_canonical_json(envelope)))
    elapsed_seconds = time.perf_counter() - started_at
    _, python_heap_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    memory_after = _process_memory()
    sorted_latencies = sorted(latencies_ms)
    p95_index = min(len(sorted_latencies) - 1, int(len(sorted_latencies) * 0.95))
    return {
        "runtime": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
        "workload": {
            "iterations": iterations,
            "accounts_per_envelope": accounts_per_envelope,
        },
        "results": {
            "elapsed_seconds": round(elapsed_seconds, 6),
            "envelopes_per_second": round(iterations / elapsed_seconds, 2),
            "latency_ms_median": round(statistics.median(latencies_ms), 4),
            "latency_ms_p95": round(sorted_latencies[p95_index], 4),
            "encoded_envelope_bytes_median": int(statistics.median(encoded_sizes)),
            "python_heap_peak_bytes": python_heap_peak,
            "rss_before_bytes": memory_before["rss_bytes"],
            "rss_after_bytes": memory_after["rss_bytes"],
            "process_peak_rss_bytes": memory_after["peak_rss_bytes"],
        },
        "limitations": [
            "This measures only in-process envelope cryptography with synthetic data.",
            "It does not include TLS, WebSocket, database, container, or network overhead.",
            "Python prototype memory is not a forecast for a Go production sidecar.",
        ],
    }


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolProbeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
