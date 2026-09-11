"""
The NVIDIA client, its key pool, and its failure handling.

Every test here is offline. The point of the module is what it does when the
network misbehaves, and "429 then 200" is not something a live run produces on
demand -- so the transport is mocked and the *decisions* are what is tested:
which failures are retried, which are not, when a key rotates, when it leaves
the rotation for good, and whether a key can ever reach an error message.
"""

import io
import json
import urllib.error

import pytest

from src.common.llm import LLMError
from src.common.nvidia import (
    MODELS,
    KeyPool,
    ModelPool,
    ModelUnavailable,
    NoUsableKey,
    NVIDIAClient,
    NVIDIASettings,
    load_nvidia_settings,
    model_for,
    redact,
)
from src.common.retry import RetryPolicy

# Deliberately NOT shaped like a real NVIDIA key. `nvapi-` is the real prefix,
# and a repository-wide grep for it is one of the cheapest credential checks
# there is -- see tests/test_no_secrets.py. A realistic-looking fixture would
# make that check noisy, and a noisy check gets ignored.
FAKE_KEYS = (
    "TEST-NOT-A-REAL-KEY-0000000001",
    "TEST-NOT-A-REAL-KEY-0000000002",
    "TEST-NOT-A-REAL-KEY-0000000003",
)


def settings(**over):
    base = dict(
        api_keys=FAKE_KEYS,
        model="nemotron",
        requests_per_minute=0,  # no pacing in tests
        max_attempts=4,
        key_cooldown_s=30.0,
    )
    base.update(over)
    return NVIDIASettings(**base)


def http_error(code: int, body: str = "", headers: dict | None = None):
    return urllib.error.HTTPError(
        url="https://integrate.api.nvidia.com/v1/chat/completions",
        code=code,
        msg="err",
        hdrs=headers or {},
        fp=io.BytesIO(body.encode()),
    )


def ok_payload(text: str = "ok"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def client_with(responses, **over):
    """A client whose transport replays `responses` in order.

    An entry that is an Exception is raised; anything else is returned as the
    decoded body. `seen` records the key slot each attempt used, which is how
    rotation is observed without ever comparing key values.
    """
    c = NVIDIAClient(
        settings(**over),
        policy=RetryPolicy(max_attempts=4, base_delay=0.0, max_delay=0.0, sleep=lambda s: None),
    )
    queue = list(responses)
    seen: list[int] = []

    def fake_post(body):
        index, _key = c.pool.current()
        seen.append(c.pool.states[index].slot)
        item = queue.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            # THE REAL RULE, not a copy of it: the client's own handler decides
            # what is retried and what takes a key out of the rotation.
            c._raise_for_http(item, index)
        if isinstance(item, Exception):
            raise item
        return item

    c._post = fake_post  # type: ignore[method-assign]
    c._seen = seen  # type: ignore[attr-defined]
    return c


# --- the registry -------------------------------------------------------------


def test_all_three_requested_models_are_registered_by_verified_id():
    """The three models the evaluation was specified against are present, under
    the identifier the API accepts, and the retired one is marked rather than
    silently swapped."""
    assert MODELS["minimax"].model_id == "minimaxai/minimax-m3"
    assert MODELS["nemotron"].model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert MODELS["deepseek"].model_id == "deepseek-ai/deepseek-v4-flash-0731"
    assert MODELS["minimax"].status == "retired"
    assert "end of life" in MODELS["minimax"].status_detail


def test_model_lookup_accepts_handle_or_full_id():
    assert model_for("nemotron").handle == "nemotron"
    assert model_for("nvidia/nemotron-3.5-lightning-30b-a3b").handle == "nemotron"
    with pytest.raises(ValueError):
        model_for("gpt-9")


def test_constructing_a_client_for_a_retired_model_raises_rather_than_substituting():
    with pytest.raises(ModelUnavailable) as exc:
        NVIDIAClient(settings(model="minimax"))
    assert "end of life" in str(exc.value)


# --- secrets ------------------------------------------------------------------


def test_redact_removes_keys_from_any_string():
    text = f"failed with {FAKE_KEYS[0]} at the end"
    assert FAKE_KEYS[0] not in redact(text, FAKE_KEYS)
    assert "***REDACTED***" in redact(text, FAKE_KEYS)


def test_redact_ignores_short_strings_so_it_cannot_mangle_ordinary_text():
    assert redact("a short a", ("a",)) == "a short a"


def test_fingerprint_never_contains_a_key():
    printed = json.dumps(settings().fingerprint())
    for key in FAKE_KEYS:
        assert key not in printed
    assert "api_keys_configured" in printed


def test_error_bodies_are_redacted_before_they_reach_an_exception():
    leaky = json.dumps({"detail": f"bad token {FAKE_KEYS[0]}"})
    c = client_with([http_error(400, leaky)])
    with pytest.raises(LLMError) as exc:
        c.generate("hi")
    assert FAKE_KEYS[0] not in str(exc.value)


def test_key_pool_report_carries_slots_not_keys():
    pool = KeyPool(keys=FAKE_KEYS)
    pool.current()
    printed = json.dumps(pool.report())
    for key in FAKE_KEYS:
        assert key not in printed
    assert '"slot": 1' in printed


# --- the key pool -------------------------------------------------------------


def test_pool_is_sticky_and_does_not_rotate_per_request():
    """Three calls, no failures, one key. Rotating here would be using three
    accounts to go faster, which is not what the pool is for."""
    pool = KeyPool(keys=FAKE_KEYS)
    slots = [pool.current()[0] for _ in range(3)]
    assert slots == [0, 0, 0]
    assert pool.rotations == 0


def test_cooling_a_key_moves_to_the_next_one_and_it_comes_back():
    now = [0.0]
    pool = KeyPool(keys=FAKE_KEYS, cooldown_s=10.0, clock=lambda: now[0])
    assert pool.current()[0] == 0
    pool.cool(0)
    assert pool.current()[0] == 1
    now[0] = 11.0
    pool.cool(1)
    # Slot 1 is now the one cooling; slot 0's cooldown has expired.
    assert pool.current()[0] == 2


def test_invalidating_a_key_removes_it_permanently():
    now = [0.0]
    pool = KeyPool(keys=FAKE_KEYS, cooldown_s=1.0, clock=lambda: now[0])
    pool.invalidate(0, "HTTP 401")
    now[0] = 1000.0
    assert pool.current()[0] == 1
    assert pool.usable_count() == 2
    assert pool.states[0].invalid


def test_exhausted_pool_raises_with_a_diagnosis_and_no_key_material():
    pool = KeyPool(keys=FAKE_KEYS)
    for i in range(3):
        pool.invalidate(i, "HTTP 401: bad key")
    with pytest.raises(NoUsableKey) as exc:
        pool.current()
    message = str(exc.value)
    assert "permanently rejected" in message
    for key in FAKE_KEYS:
        assert key not in message


# --- retry and fallback -------------------------------------------------------


def test_429_cools_the_key_rotates_and_succeeds_on_the_next_one():
    c = client_with([http_error(429, '{"detail":"rate limited"}'), ok_payload()])
    response = c.generate("hi")
    assert response.text == "ok"
    assert c._seen == [1, 2]              # rotated after the 429
    assert c.stats.rate_limited == 1
    assert c.stats.retries == 1
    assert c.pool.states[0].rate_limits == 1


def test_transient_5xx_is_retried_on_the_same_key():
    """A 503 is the service, not the key. Rotating on it would burn a second
    account's quota to work around the first one's outage."""
    c = client_with([http_error(503, '{"detail":"upstream"}'), ok_payload()])
    assert c.generate("hi").text == "ok"
    assert c._seen == [1, 1]
    assert c.stats.transient_errors == 1


def test_a_read_timeout_is_retried_rather_than_escaping():
    """Drives the real `_post`, socket and all, so the OSError clause is the
    thing under test. A bare TimeoutError is an OSError but not a URLError, and
    D-020 records what it cost on the Gemini path when only URLError was
    caught: a recoverable network stall killed a live run four calls in."""
    import src.common.nvidia as nv

    c = NVIDIAClient(
        settings(),
        policy=RetryPolicy(
            max_attempts=3, base_delay=0.0, max_delay=0.0, sleep=lambda s: None
        ),
    )
    calls = {"n": 0}

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(ok_payload()).encode()

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return Fake()

    original = nv.urllib.request.urlopen
    nv.urllib.request.urlopen = fake_urlopen
    try:
        assert c.generate("hi").text == "ok"
    finally:
        nv.urllib.request.urlopen = original
    assert calls["n"] == 2
    assert c.stats.transient_errors == 1


def test_a_stalling_model_uses_its_own_shorter_timeout():
    """DeepSeek's endpoint stalls rather than erroring, so its registry entry
    caps the per-request timeout well below the global one. Without this a
    single dead model holds five attempts for the full global timeout."""
    import src.common.nvidia as nv

    seen: list[float] = []
    c = NVIDIAClient(settings(timeout_s=300.0), model="deepseek")

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(ok_payload()).encode()

    def fake_urlopen(request, timeout=None):
        seen.append(timeout)
        return Fake()

    original = nv.urllib.request.urlopen
    nv.urllib.request.urlopen = fake_urlopen
    try:
        c.generate("hi")
    finally:
        nv.urllib.request.urlopen = original
    assert seen == [90.0]


def test_401_takes_the_key_out_and_the_call_continues_on_the_next():
    c = client_with([http_error(401, '{"detail":"invalid key"}'), ok_payload()])
    assert c.generate("hi").text == "ok"
    assert c.pool.states[0].invalid
    assert c.pool.usable_count() == 2


def test_every_key_rejected_ends_the_call_rather_than_retrying_forever():
    c = client_with([http_error(401, "bad") for _ in range(3)])
    with pytest.raises((NoUsableKey, RuntimeError)) as exc:
        c.generate("hi")
    # Three attempts, one per key, and then it stops -- not max_attempts.
    assert len(c._seen) == 3
    assert "key" in str(exc.value).lower()


def test_400_is_never_retried():
    """A malformed request will be malformed next time. Five attempts on it
    only bury the message that explains what was wrong."""
    c = client_with([http_error(400, '{"detail":"bad field"}'), ok_payload()])
    with pytest.raises(LLMError) as exc:
        c.generate("hi")
    assert len(c._seen) == 1
    assert "bad field" in str(exc.value)


def test_404_unknown_model_is_never_retried():
    c = client_with([http_error(404, '{"detail":"model not found"}')])
    with pytest.raises(LLMError):
        c.generate("hi")
    assert len(c._seen) == 1


def test_410_reports_the_model_as_gone_not_as_a_bad_request():
    c = client_with([http_error(410, '{"detail":"end of life"}')])
    with pytest.raises(ModelUnavailable):
        c.generate("hi")
    assert len(c._seen) == 1


def test_retry_after_header_is_honoured():
    slept: list[float] = []
    c = NVIDIAClient(
        settings(),
        policy=RetryPolicy(
            max_attempts=3, base_delay=0.0, max_delay=0.0, sleep=slept.append
        ),
    )
    queue = [http_error(429, "{}", {"Retry-After": "7"}), ok_payload()]

    def fake_post(body):
        index, _ = c.pool.current()
        item = queue.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            c._raise_for_http(item, index)
        return item

    c._post = fake_post  # type: ignore[method-assign]
    c.generate("hi")
    assert slept and max(slept) >= 7.0


# --- response handling --------------------------------------------------------


def test_token_counts_come_off_the_response():
    c = client_with([ok_payload()])
    r = c.generate("hi")
    assert (r.prompt_tokens, r.output_tokens, r.total_tokens) == (10, 5, 15)
    assert c.total_tokens == 15
    assert c.calls == 1


def test_an_empty_answer_is_resampled_and_then_fails_loudly():
    """An empty agent output looks like a legitimate result once it is in a
    trace, so a truncated or filtered answer must never be returned. It is
    retried first -- `length` and a content filter are both things a second
    sample can get past -- and when it keeps happening the call fails."""
    truncated = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    c = client_with([truncated] * 4)
    with pytest.raises(RuntimeError) as exc:
        c.generate("hi")
    assert len(c._seen) == 4, "it should have resampled, not given up at once"
    assert "length" in str(exc.value)


def test_degenerate_json_is_resampled_rather_than_killing_the_run():
    """MEASURED, not hypothetical. Nemotron 3.5 Lightning answered a JSON
    request with an opening brace and ~3000 tab characters -- temperature 0,
    finish_reason=stop, plausible token count -- and the same prompt a minute
    later returned correct JSON. As an LLMError that killed a live pipeline at
    its second call. It is a transient generation failure and the retry loop is
    in the client, so it is handled there."""
    garbage = {
        "choices": [{"message": {"content": "{" + "	" * 3000}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 700, "total_tokens": 740},
    }
    good = {
        "choices": [
            {"message": {"content": '{"brief": "x", "questions": []}'},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
    }
    c = client_with([garbage, good])
    response = c.generate("hi", json_output=True)
    assert response.json() == {"brief": "x", "questions": []}
    assert c.stats.malformed_json == 1
    # The rejected attempt's tokens were spent and are counted, not forgotten.
    assert c.total_tokens == 740 + 60
    # And they reach the CALLER, not just the stats -- the trace's usage record
    # is built from this response, and the cost metric splits recovery into
    # analysis and replay. A resampled call reporting only its final attempt
    # would make the analysis look cheaper than it was, in our own favour.
    assert response.total_tokens == 740 + 60
    assert response.prompt_tokens == 40 + 40
    assert response.output_tokens == 700 + 20
    assert response.attempts == 2


def test_a_model_that_never_returns_json_fails_after_a_bounded_number_of_tries():
    garbage = {
        "choices": [{"message": {"content": "not json at all"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 10},
    }
    c = client_with([garbage] * 4)
    with pytest.raises(RuntimeError):
        c.generate("hi", json_output=True)
    assert len(c._seen) == 4
    assert c.stats.malformed_json == 4


def test_a_fenced_json_answer_is_accepted_because_the_caller_can_read_it():
    fenced = {
        "choices": [
            {
                "message": {"content": '```json\n{"a": 1}\n```'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 10},
    }
    c = client_with([fenced])
    assert c.generate("hi", json_output=True).json() == {"a": 1}
    assert c.stats.malformed_json == 0


def test_plain_prose_is_untouched_when_json_was_not_asked_for():
    c = client_with([ok_payload("just some prose")])
    assert c.generate("hi").text == "just some prose"
    assert c.stats.malformed_json == 0


def test_nemotron_request_disables_the_visible_chain_of_thought():
    sent: dict = {}
    c = client_with([ok_payload()])
    inner = c._post

    def capture(body):
        sent.update(body)
        return inner(body)

    c._post = capture  # type: ignore[method-assign]
    c.generate("hi", system="terse")
    assert sent["chat_template_kwargs"] == {"thinking": False}
    assert "response_format" not in sent
    assert sent["messages"][0]["role"] == "system"
    assert sent["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_json_mode_is_requested_when_the_caller_asks_for_json():
    """Measured: with `thinking` off, JSON mode returns clean JSON in ~4s;
    with thinking on it spends the whole budget narrating and returns prose."""
    sent: dict = {}
    c = client_with([{"choices": [{"message": {"content": "{}"},
                                   "finish_reason": "stop"}]}])
    inner = c._post

    def capture(body):
        sent.update(body)
        return inner(body)

    c._post = capture  # type: ignore[method-assign]
    c.generate("hi", json_output=True)
    assert sent["response_format"] == {"type": "json_object"}


# --- model pool ---------------------------------------------------------------


def test_model_pool_skips_retired_models():
    pool = ModelPool(settings())
    assert "minimax" not in pool.candidates()
    assert "nemotron" in pool.candidates()


def test_model_pool_falls_back_when_one_model_fails():
    pool = ModelPool(settings())
    tried: list[str] = []

    def work(client):
        tried.append(client.spec.handle)
        if client.spec.handle == "nemotron":
            raise LLMError("model is sulking")
        return "done"

    value, used = pool.run(work)
    assert value == "done"
    assert used == "deepseek"
    assert tried == ["nemotron", "deepseek"]
    assert pool.failures["nemotron"] == 1


def test_a_cooled_model_is_not_offered_again_until_its_timer_expires():
    now = [0.0]
    pool = ModelPool(settings(model_cooldown_s=100.0), clock=lambda: now[0])
    pool.cool("nemotron")
    assert "nemotron" not in pool.candidates()
    now[0] = 101.0
    assert "nemotron" in pool.candidates()


def test_model_pool_raises_when_everything_is_unavailable():
    pool = ModelPool(settings(model_cooldown_s=1000.0))
    for handle in pool.candidates():
        pool.cool(handle)
    with pytest.raises(ModelUnavailable):
        pool.run(lambda client: "never")


def test_a_dry_key_pool_is_not_treated_as_a_model_failure():
    """No other model will do better with no usable key, so the pool must not
    walk the whole model list burning a request each."""
    pool = ModelPool(settings())
    for index in range(3):
        pool.pool.invalidate(index, "HTTP 401")
    with pytest.raises(NoUsableKey):
        pool.run(lambda client: client.generate("hi"))


# --- settings loading ---------------------------------------------------------


def test_settings_load_the_three_keys_from_an_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                f"NVIDIA_API_KEY_1={FAKE_KEYS[0]}",
                f"NVIDIA_API_KEY_2={FAKE_KEYS[1]}",
                f"NVIDIA_API_KEY_3={FAKE_KEYS[2]}",
                "NVIDIA_MAX_CONCURRENCY=3",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_nvidia_settings(env)
    assert loaded.key_count() == 3
    assert loaded.max_concurrency == 3


def test_missing_keys_raise_with_instructions(tmp_path, monkeypatch):
    for name in ("NVIDIA_API_KEY_1", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY_3"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=x", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        load_nvidia_settings(env)
    assert "NVIDIA_API_KEY_1" in str(exc.value)


def test_a_partly_configured_pool_still_works(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"NVIDIA_API_KEY_2={FAKE_KEYS[1]}", encoding="utf-8")
    loaded = load_nvidia_settings(env)
    assert loaded.key_count() == 1


def test_json_requests_use_a_smaller_token_budget():
    """A degenerate JSON answer is whitespace until the budget runs out, so the
    budget is what a rejected attempt costs. Every JSON answer this project
    asks for is small, so the large ceiling buys nothing here."""
    sent: dict = {}
    c = client_with([{"choices": [{"message": {"content": "{}"},
                                   "finish_reason": "stop"}]},
                     ok_payload()])
    inner = c._post

    def capture(body):
        sent.clear()
        sent.update(body)
        return inner(body)

    c._post = capture  # type: ignore[method-assign]
    c.generate("hi", json_output=True)
    small = sent["max_tokens"]
    c.generate("hi")
    assert small < sent["max_tokens"]
    assert small == MODELS["nemotron"].json_max_tokens


def test_a_repeatedly_failing_model_is_left_alone_for_longer_each_time():
    """MEASURED. With a flat cooldown, a six-scenario generation pass preferred
    a dead endpoint three times, and each attempt cost five bounded requests
    that stall for 90 seconds apiece. The cooldown expired between scenarios
    every time, so the pool tried again, and again."""
    now = [0.0]
    pool = ModelPool(settings(model_cooldown_s=100.0), clock=lambda: now[0])

    pool.cool("deepseek")                       # 1st failure: out for 100s
    now[0] = 99.0
    assert "deepseek" not in pool.candidates()
    now[0] = 101.0
    assert "deepseek" in pool.candidates(), "one bad minute should not exile it"

    pool.cool("deepseek")                       # 2nd failure: out for 200s
    now[0] = 250.0
    assert "deepseek" not in pool.candidates(), "the window should have doubled"
    now[0] = 302.0
    assert "deepseek" in pool.candidates()

    pool.cool("deepseek")                       # 3rd failure: out for 400s
    now[0] = 600.0
    assert "deepseek" not in pool.candidates()
    assert pool.failures["deepseek"] == 3

    # By the time it has failed six times it is out for the rest of any
    # campaign, rather than costing another seven minutes every few scenarios.
    for _ in range(3):
        pool.cool("deepseek")
    remaining = pool.cooldown_until["deepseek"] - now[0]
    assert remaining > 1000


def test_the_cooldown_does_not_grow_without_bound():
    from src.common.nvidia import MAX_MODEL_COOLDOWN_S

    now = [0.0]
    pool = ModelPool(settings(model_cooldown_s=100.0), clock=lambda: now[0])
    for _ in range(30):
        pool.cool("deepseek")
    assert pool.cooldown_until["deepseek"] - now[0] <= MAX_MODEL_COOLDOWN_S


def test_preflight_cools_a_model_that_cannot_answer():
    """Two short requests before a campaign commits, rather than discovering a
    dead endpoint three scenarios in at five stalled requests apiece."""
    import src.common.nvidia as nv

    pool = ModelPool(settings())
    seen: list[tuple[str, bool]] = []

    def fake_generate(self, prompt, system=None, json_output=False, temperature=None):
        if self.spec.handle == "deepseek":
            raise RuntimeError("gave up after 1 attempts: network error: TimeoutError")
        from src.common.llm import LLMResponse

        return LLMResponse(
            text="ok", model=self.spec.model_id, prompt_tokens=5, output_tokens=1,
            thoughts_tokens=0, total_tokens=6, attempts=1, latency_s=0.1,
            slept_s=0.0, finish_reason="stop",
        )

    original = nv.NVIDIAClient.generate
    nv.NVIDIAClient.generate = fake_generate
    try:
        result = pool.preflight(on_result=lambda h, ok, why: seen.append((h, ok)))
    finally:
        nv.NVIDIAClient.generate = original

    assert result == {"nemotron": True, "deepseek": False}
    assert ("deepseek", False) in seen
    assert "deepseek" not in pool.candidates()
    assert "nemotron" in pool.candidates()


def test_preflight_uses_a_short_deadline_of_its_own():
    """A model that needs the full campaign timeout to say "ok" is not a model
    the campaign can afford, so the probe does not wait that long."""
    import src.common.nvidia as nv

    pool = ModelPool(settings(timeout_s=300.0))
    seen: list[float] = []

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(ok_payload()).encode()

    def fake_urlopen(request, timeout=None):
        seen.append(timeout)
        return Fake()

    original = nv.urllib.request.urlopen
    nv.urllib.request.urlopen = fake_urlopen
    try:
        pool.preflight(timeout_s=20.0)
    finally:
        nv.urllib.request.urlopen = original
    assert seen and max(seen) <= 20.0


def test_a_clients_trace_header_names_the_model_it_actually_uses():
    """D-057's bug, one level down. The settings fingerprint carries the
    configured *default* model, and `ModelPool.client_for()` routinely points a
    client at a different one. Without the client's own override, a DeepSeek
    client wrote `model: nvidia/nemotron-...` into its trace header -- and a
    per-model comparison built on that header would attribute every run to
    whichever model happened to be the default."""
    s = settings(model="nemotron")
    assert s.fingerprint()["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"

    ds = NVIDIAClient(s, model="deepseek")
    assert ds.fingerprint()["model"] == "deepseek-ai/deepseek-v4-flash-0731"
    assert ds.fingerprint()["model_handle"] == "deepseek"
    # The fields it does not own are inherited unchanged.
    assert ds.fingerprint()["temperature"] == s.temperature
    assert ds.fingerprint()["provider"] == "nvidia"

    nm = NVIDIAClient(s, model="nemotron")
    assert nm.fingerprint()["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_a_client_fingerprint_still_carries_no_key():
    printed = json.dumps(NVIDIAClient(settings(), model="deepseek").fingerprint())
    for key in FAKE_KEYS:
        assert key not in printed


def test_the_model_cooldown_is_scaled_to_what_a_failed_test_costs():
    """MEASURED. 120s was chosen to match the key cooldown, and the two
    failures do not cost the same: a rate-limited key costs one request, a
    model that will not serve a whole test costs 22 minutes of stalled retries.
    A 120s window expired between one test and the next, so the campaign handed
    the same dead endpoint the very next test it had."""
    from src.common.nvidia import NVIDIASettings

    default = NVIDIASettings(api_keys=("TEST-NOT-A-REAL-KEY-0000000001",))
    assert default.model_cooldown_s >= 600, (
        "a model cooldown shorter than the cost of the failure it follows lets "
        "a dead endpoint back in before the next test starts"
    )
    assert default.model_cooldown_s > default.key_cooldown_s


# --- the setup check ----------------------------------------------------------
#
# The first five minutes with a research repo decide whether anyone runs it, and
# every failure in those five minutes otherwise looks like a stack trace. These
# pin the two things that matter: it exits non-zero when setup is incomplete,
# and it never prints a key.


def test_doctor_reports_a_missing_env_file_with_a_fix(tmp_path, monkeypatch, capsys):
    import src.common.nvidia as nv

    monkeypatch.chdir(tmp_path)
    for name in nv.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    assert nv.doctor() == 1
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert ".env.example" in out, "it must say what to copy"
    assert "build.nvidia.com" in out, "it must say where a key comes from"


def test_doctor_reports_an_env_file_with_no_keys(tmp_path, monkeypatch, capsys):
    import src.common.nvidia as nv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# nothing yet\n", encoding="utf-8")
    for name in nv.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    assert nv.doctor() == 1
    out = capsys.readouterr().out
    assert "NONE FOUND" in out
    assert "NVIDIA_API_KEY_1" in out
    assert "One key is enough" in out, "a newcomer must not think three are required"


def test_doctor_never_prints_a_key(tmp_path, monkeypatch, capsys):
    import src.common.nvidia as nv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"NVIDIA_API_KEY_1={FAKE_KEYS[0]}\n", encoding="utf-8"
    )
    for name in nv.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        nv.ModelPool, "preflight",
        lambda self, on_result=None, timeout_s=45.0: (
            on_result("nemotron", True, "") if on_result else None
        ) or {"nemotron": True},
    )
    nv.doctor()
    out = capsys.readouterr().out
    for key in FAKE_KEYS:
        assert key not in out
    assert "never printed" in out


def test_doctor_succeeds_and_names_the_next_command(tmp_path, monkeypatch, capsys):
    import src.common.nvidia as nv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"NVIDIA_API_KEY_1={FAKE_KEYS[0]}\n", encoding="utf-8"
    )
    for name in nv.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        nv.ModelPool, "preflight",
        lambda self, on_result=None, timeout_s=45.0: (
            on_result("nemotron", True, "") if on_result else None
        ) or {"nemotron": True},
    )
    assert nv.doctor() == 0
    out = capsys.readouterr().out
    assert "READY" in out
    assert "real_campaign --plan" in out, "the cheapest next step must be offered first"
    assert "docs/09" in out, "and the caveats must be pointed at"


def test_doctor_fails_when_no_model_answers(tmp_path, monkeypatch, capsys):
    import src.common.nvidia as nv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"NVIDIA_API_KEY_1={FAKE_KEYS[0]}\n", encoding="utf-8"
    )
    for name in nv.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        nv.ModelPool, "preflight",
        lambda self, on_result=None, timeout_s=45.0: (
            on_result("nemotron", False, "timeout") if on_result else None
        ) or {"nemotron": False},
    )
    assert nv.doctor() == 1
    out = capsys.readouterr().out
    assert "No model answered" in out
    assert "this is the provider, not you" in out
    assert "src.eval.experiment" in out, "the offline path still works and must be offered"
