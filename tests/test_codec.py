"""Payload encryption: Temporal stores only ciphertext; workers, the console and the codec
endpoint (for the Temporal UI) decrypt with the key."""

from __future__ import annotations

import base64
import os

import httpx
from temporalio.client import Client

from soc import api
from soc.codec import ENCODING, EncryptionCodec, data_converter
from soc.history import HistoryBars
from soc.models import CaseInput, Status
from soc.worker import SocWorker
from soc.workflow import CaseInvestigationWorkflow


async def test_history_holds_ciphertext_and_the_console_decrypts(env, settings):
    codec = EncryptionCodec(os.urandom(32))
    secure = Client(env.client.service_client, data_converter=data_converter(codec))
    async with SocWorker(secure, "codec-q"):
        handle = await secure.start_workflow(
            CaseInvestigationWorkflow.run,
            CaseInput("auto-fp-01"),
            id="case-codec",
            task_queue="codec-q",
        )
        assert (await handle.result()).status == Status.AUTO_RESOLVED

    raw = [e async for e in env.client.get_workflow_handle("case-codec").fetch_history_events()]
    stored = raw[0].workflow_execution_started_event_attributes.input.payloads[0]
    assert stored.metadata["encoding"] == ENCODING and b"auto-fp-01" not in stored.data

    bars = await HistoryBars(secure).build(raw)  # the console holds the key
    fetch = next(b for b in bars if b.get("name") == "fetch_alert")
    assert fetch["input"]["scenario"] == "auto-fp-01"


async def test_codec_endpoint_round_trip(monkeypatch):
    monkeypatch.setenv("SOC_CODEC_KEY", base64.b64encode(os.urandom(32)).decode())
    plain = {"payloads": [{"metadata": {"encoding": "anNvbi9wbGFpbg=="}, "data": "eyJhIjoxfQ=="}]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(api.app), base_url="http://t"
    ) as http:
        sealed = (await http.post("/codec/encode", json=plain)).json()
        assert sealed["payloads"][0]["data"] != plain["payloads"][0]["data"]
        opened = (await http.post("/codec/decode", json=sealed)).json()
    assert opened == plain
