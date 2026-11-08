# Hosted Embeddings (#78) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test Voyage and Cohere on the same questions Qwen failed, write up the result, and add the winner behind a small embedding interface that #79 can call.

**Architecture:** Two stages, and stage 2 depends on stage 1's result. Stage 1 reruns the original 2026-10-17 Qwen test against four hosted models and picks a winner by a rule fixed here, before any numbers exist. Stage 2 adds an `EmbeddingProvider` Protocol and one provider over the project's existing `httpx`, modelled on `providers/transcription/soniox.py`.

**Tech Stack:** Python 3.14, `httpx`, pydantic-settings, pytest with `httpx.MockTransport`. No new dependencies.

**Spec:** GitHub issue #78 (`gh issue view 78`). Run order and constraints: `docs/superpowers/plans/2026-11-03-free-remi-roadmap.md` in the main checkout.

## Needed from the user at plan review

**D1. Confirm the test set is invented.** This repo is public, and the set reads like one family's details (names, a school, an allergy, a doctor). The evidence says Claude made it up: those names appear in none of the 40 saved sessions' typed messages, nor in the repo or memory. But only the user can be sure. It decides two things: whether the set may be committed (Task 1), and whether it may be sent to the vendors (Task 2). **Task 2 does not start without this answer.** For that reason the set's text is not in this plan.

## Global Constraints

- **Secrets.** Key values are never printed, logged or written to a tracked file; one has already leaked into a transcript. Never run `cat .env`, `env`, `printenv`, `set -x`, `echo $…KEY`, or `curl` with a key. Check a key exists only with `grep -c`. Print HTTP status codes only, never a response body or request headers: a 401 body can quote part of the key.
- The app's real settings are `EMBEDDING__API_KEY`, `EMBEDDING__REQUEST_TIMEOUT_S`, `MODELS__EMBEDDING_MODEL` and `MODELS__EMBEDDING_DIMENSIONS`. `VOYAGE_API_KEY` and `COHERE_API_KEY` are provisional: only the bake-off script and the Task 3 smoke check read them, and `Settings` ignores them (`extra="ignore"`).
- Dimension is **1024**: every candidate supports it, and it is under pgvector's 2000-dimension limit for HNSW on a `vector` column.
- Scope: no DDL, no `skills` table (#79), no notes search, no agent or prompt changes. No live LLM evals are needed, because no prompt changes.
- Keep git commands plain and separate. This worktree's guard refuses complex ones and may refuse long inline scripts, so save those as files. Add files by name; never `git add -A`. Commit messages end with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Worktree tests need `PYTHONPATH=src`. Test DB `po_t78` is ready.
- After code changes run `PYTHONHASHSEED=0 graphify update .`. `graphify-out/` is not gitignored here, so never add it.

## Review Focus

Each has its test in Task 3.

1. The provider answers with too few vectors or the wrong length: refused, so a bad vector never reaches `vector(N)`.
2. Voyage returns `data` out of order: each vector still lines up with its text (Voyage only; Cohere's list is positional).
3. A vendor's error body echoes the text: it reaches neither the exception, nor `__cause__`, nor the logs.
4. An empty list: returns `[]` with no HTTP call, since both vendors reject it.
5. Throttled (429) or down: a retryable error after exactly one try, so the job layer stays the only retry.

---

## Stage 1: The bake-off

### Why the set is not being rebuilt

The original test survives in the session transcript that ran it. The same 20 facts and 20 questions were run three ways, top 3 (top 1 in brackets), on Qwen3-Embedding-0.6B:

| Condition | hit@3 | hit@1 |
|---|---|---|
| All English | 20/20 | 18/20 |
| All Hebrew | 13/20 | 9/20 |
| Mixed, as people type | 13/20 | 11/20 |
| ...mixed, same language | 12/13 | not recorded |
| ...mixed, Hebrew-English | 1/7 | not recorded |

Rerunning the identical set makes these numbers directly comparable, so no Qwen control run is needed. Corpus size was 20 notes in each condition.

**Why the script is kept (`scripts/embedding_bakeoff.py`), not thrown away:** this test was deleted once and had to be dug out of a transcript. A model retirement will need it again, and it costs about 100 lines that no production code imports.

### Candidates

Tested at 1024 dimensions. `voyage-4-lite` and `embed-v5.0-fast` are left out: the first is not described as multilingual, and the second is the cheaper sibling of a model already tested.

| Model | Vendor | Sends `output_dimension` | Why |
|---|---|---|---|
| `voyage-4-large` | Voyage | yes | Voyage's best multilingual, $0.12/M |
| `voyage-4` | Voyage | yes | Same family at half the price, $0.06/M |
| `embed-v5.0-pro` | Cohere | yes | Cohere's newest. Docs say 100+ languages but give no list, so Hebrew is unconfirmed |
| `embed-multilingual-v3.0` | Cohere | no (fixed 1024) | The one Cohere model documented as covering Hebrew (`he`) |

### How the winner is picked

Fixed before any numbers exist. All counts are hit@3 unless stated.

A model **qualifies** (clearly beats Qwen on the identical set) only if all of these hold:

- English at least 19/20 (Qwen 20: one miss is noise)
- All Hebrew at least 17/20 (Qwen 13)
- Mixed same-language at least 12/13 (Qwen 12: no regression)
- Hebrew-English at least 5/7 (Qwen 1)

**Score** = all-Hebrew hits + Hebrew-English hits, out of 27 (Qwen 14). English is only a gate, because it was never the problem.

1. **No model qualifies: STOP.** Neither clearly beats Qwen. Do not start stage 2.
2. **One qualifies:** it wins.
3. **Several qualify:** the highest score wins if it leads the next by 2 or more. With only 27 questions, a gap of 1 is noise.
4. **Tied (within 1):** among those tied, more hit@1 hits across all-Hebrew and mixed wins, again by 2 or more.
5. **Still tied:** a list price at least 25% lower wins. Only use prices from the vendor's own page.
6. **Still tied: STOP**, with a recommendation.

**Also STOP if:**

- A model keeps failing after its request is checked against the table in Task 3. Do not publish a partial table or silently swap a model.
- The winner's vendor offers no way to opt out of training on API data, or the terms cannot be confirmed. People's words would go there.

**On STOP:** commit the write-up with status "awaiting decision", report the numbers and options to the user, and do not start Task 3.

### Task 1: Recover the set and build the script

**Files:**
- Create: `scripts/embedding_bakeoff.py`
- Create: `scripts/embedding_bakeoff_set.json` (committed only if D1 is confirmed)

**Interfaces:**
- Produces: `uv run python scripts/embedding_bakeoff.py [--check]`. The set's JSON shape is `{"english"|"hebrew"|"mixed": {"notes": [str, ...], "questions": [[str, int], ...]}}`, where the int indexes `notes`.

- [ ] **Step 1: Recover the set**

Save this as `/tmp/recover_78.py`. It was dry-run against the transcript on 2026-11-08 (20 notes and 20 questions per condition, 7 cross-language in mixed).

```python
import ast, json, pathlib, re, sys

root = pathlib.Path.home() / ".claude/projects/-home-daniels-Projects-personal-organizer"
path = root / "bc02c8f6-d962-4e28-b92b-c425e08db978.jsonl"
blocks = []
for line in path.read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    if rec.get("type") != "assistant":
        continue
    for part in rec["message"]["content"]:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            blocks.append(part["input"].get("content") or part["input"].get("command") or "")

def grab(name):
    for block in blocks:
        for match in re.finditer(rf"(?m)^{name} = \[", block):
            start = match.end() - 1
            for end in (i + 1 for i, ch in enumerate(block) if i >= start and ch == "]"):
                try:
                    value = ast.literal_eval(block[start:end])
                except (SyntaxError, ValueError):
                    continue
                if len(value) == 20:
                    return value
                break
    raise SystemExit(f"{name} not found")

data = {
    "english": {"notes": grab("N"), "questions": grab("Q")},
    "hebrew": {"notes": grab("NOTES_HE"), "questions": grab("Q_HE")},
    "mixed": {"notes": grab("NOTES"), "questions": grab("QUERIES")},
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
```

Run: `python3 -I /tmp/recover_78.py /home/daniels/Projects/personal-organizer/.claude/worktrees/feat-78-hosted-embeddings/scripts/embedding_bakeoff_set.json`
Expected: no output, and the file exists. Do not print the file's contents.

- [ ] **Step 2: Write `scripts/embedding_bakeoff.py`**

A sync script using `httpx`. It writes with `sys.stdout.write`, since ruff bans `print` (T20).

- `Candidate(model: str, vendor: Literal["voyage", "cohere"], sends_dimension: bool)`, one per row of the table above.
- `is_hebrew(text: str) -> bool`: any character in U+0590 to U+05FF.
- `rank(question: Sequence[float], notes: Sequence[Sequence[float]]) -> list[int]`: note indexes, best first, by true cosine (divide by the norms; don't assume normalised vectors).
- `embed(client, candidate, texts, kind) -> tuple[list[list[float]], int]`: request and response per the table in Task 3, with `kind` of `document` for notes and `query` for questions. It returns vectors and tokens. On 429, sleep 25 s and retry, at most 5 times (Voyage doesn't document its limit for accounts with no card). Any other non-2xx: `raise SystemExit(f"{candidate.model}: HTTP {status}")`, with nothing from the body.
- Keys: `os.environ["VOYAGE_API_KEY"]` and `os.environ["COHERE_API_KEY"]`, read only in live mode.
- `self_check(data) -> None`, run first in every mode, using `assert`:
  - every condition has 20 notes and 20 questions
  - in `mixed`, `is_hebrew(question) != is_hebrew(note)` holds for exactly 7 questions
  - `rank([1.0, 0.0], [[0.0, 1.0], [1.0, 0.0], [0.9, 0.1]]) == [1, 2, 0]`
- `--check`: runs `self_check`, writes `self-check ok: 3 conditions, 7 cross-language questions`, exits with no network.
- Live: for each candidate and condition, one call for notes and one for questions (6 per model). Then write a Markdown table with one row per model: tokens used, English, all-Hebrew and mixed as `hit@3 (hit@1)`, mixed same-language, mixed Hebrew-English.
- Below the table, list misses by index only (`hebrew q7: wanted note 5, top note 9`), never the text, so the output can be pasted into a public doc.

- [ ] **Step 3: Run the self-check**

Run: `uv run python scripts/embedding_bakeoff.py --check`
Expected: `self-check ok: 3 conditions, 7 cross-language questions`

- [ ] **Step 4: Lint**

Run: `uv run ruff check scripts/embedding_bakeoff.py`
Run: `uv run ruff format --check scripts/embedding_bakeoff.py`
Expected: both pass. Fix what they report.

- [ ] **Step 5: Commit**

```bash
git add scripts/embedding_bakeoff.py
git commit -m "chore(embeddings): bake-off script for the Qwen test set"
```

If D1 was confirmed, also commit the set. If not, leave it untracked, and record in the write-up that it is kept locally.

```bash
git add scripts/embedding_bakeoff_set.json
git commit -m "chore(embeddings): the 20-question set the Qwen test used"
```

### Task 2: Run it, write it up, decide

**Precondition:** D1 is answered. If the set may be real, do not run this until the user has said how to proceed.

**Files:**
- Create: `docs/adr/0007-hosted-embeddings.md`

**Interfaces:**
- Produces: the ADR's `Status` line, either `Accepted: <model>` or `Awaiting decision`. Task 3 starts only on `Accepted`.

- [ ] **Step 1: Confirm both keys exist, without showing them**

Run: `grep -c '^VOYAGE_API_KEY=.' .env`
Run: `grep -c '^COHERE_API_KEY=.' .env`
Expected: `1` each.

- [ ] **Step 2: Run the bake-off**

Run: `uv run --env-file .env python scripts/embedding_bakeoff.py`
Expected: exit 0 and a table with four model rows. A run costs well under a cent. A non-zero exit shows only the model and status: check that model's request against the Task 3 table, fix, rerun. If it still fails, it's a STOP.

- [ ] **Step 3: Apply the rule**

Go through "How the winner is picked" in order, and write down each step's outcome. Don't adjust the bars after seeing results, and don't add candidates. If a different choice looks better, that's a STOP, not a judgment call.

- [ ] **Step 4: Check data terms and prices against the vendors' own pages**

For each vendor, record whether API data trains models by default, how to opt out, retention, and price per million tokens.

Known on 2026-11-08, to be confirmed:

- Voyage's public pages cover opt-out only for the MongoDB Atlas API, not `api.voyageai.com`. Find its terms for the direct API.
- Cohere: opt-out under Settings, then Data Controls; logs deleted after 30 days. Trial keys are capped at 1,000 calls a month.
- Voyage: $0.12/M (`voyage-4-large`), $0.06/M (`voyage-4`), and 200M free tokens.
- Cohere's Embed prices are not on `cohere.com/pricing`. Third parties list `embed-v5.0-pro` at $0.12/M, which must be labelled third-party if used.

- [ ] **Step 5: Write `docs/adr/0007-hosted-embeddings.md`**

Follow the existing ADRs' style. Sections:

- **Status**, as above.
- **Context:** why hosted, and that this set is the original.
- **What ran:** the four models at 1024; the hosted models use `input_type` (query or document) where Qwen used an instruction prefix.
- **Results:** the script's table with a Qwen row added, plus the misses by index.
- **Decision:** each step of the rule and its outcome, the model, and the dimension with its reasons (common to all candidates, under the HNSW limit; storage is immaterial at this scale, so smaller sizes weren't tested).
- **Cost:** monthly estimate for the circle: 50 people x 25 texts x 200 tokens = 250,000 tokens, about $0.03 at $0.12/M, against Qwen's about $24/month. State the assumptions.
- **Data use:** Step 4's findings. Say plainly that the opt-out must be set on the winner's account before real text goes there.
- **Limits:** one test set of 20 short notes, not skills; `embed-multilingual-v3.0` truncates past 512 tokens, if it wins.

No set text in this doc.

- [ ] **Step 6: Scan for leaked keys**

Save as `/tmp/scan_78.py`:

```python
import os, pathlib, subprocess, sys

keys = [os.environ[k].encode() for k in ("VOYAGE_API_KEY", "COHERE_API_KEY")]
files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], text=True).split("\n")
bad = [f for f in files if f and pathlib.Path(f).is_file() and any(k in pathlib.Path(f).read_bytes() for k in keys)]
sys.exit(f"key found in: {', '.join(bad)}" if bad else 0)
```

Run: `uv run --env-file .env python /tmp/scan_78.py`
Expected: exit 0, no output.

- [ ] **Step 7: Commit**

```bash
git add docs/adr/0007-hosted-embeddings.md
git commit -m "docs(adr): choose the hosted embedding model"
```

- [ ] **Step 8: Gate**

`Status: Accepted` means go to Task 3. `Awaiting decision` means stop here and report the numbers and options to the user.

---

## Stage 2: The chosen provider

### Decided by stage 1

Only this comes from the bake-off. Everything else in Task 3 is fixed.

| Decided | If Voyage | If Cohere |
|---|---|---|
| Model ID | `voyage-4-large` or `voyage-4` | `embed-v5.0-pro` or `embed-multilingual-v3.0` |
| Module, class | `providers/embedding/voyage.py`, `VoyageEmbedder` | `providers/embedding/cohere.py`, `CohereEmbedder` |
| `BASE_URL`, path | `https://api.voyageai.com`, `/v1/embeddings` | `https://api.cohere.com`, `/v2/embed` |
| Body: texts | `"input": [...]` | `"texts": [...]` |
| Body: kind | `"input_type": "document"` or `"query"` | `"input_type": "search_document"` or `"search_query"` |
| Body: fixed | none | `"embedding_types": ["float"]` |
| Body: dimension | `"output_dimension": N` | `"output_dimension": N`, omitted for `embed-multilingual-v3.0` |
| Vectors | `data[].embedding`, sorted by `data[].index` | `embeddings.float` |
| Tokens | `usage.total_tokens` | `int(meta.billed_units.input_tokens)` |
| Most texts per call | 1000 | 96 |

Both authenticate with `Authorization: Bearer <key>`.

**No SDK.** One POST over `httpx` is a few lines and already how Soniox works. Neither vendor's SDK would remove any code here, and each would bring its own retries to turn off.

### Task 3: Embedding interface, settings, errors and the provider

**Files:**
- Create: `src/personal_organizer/interfaces/embedding.py`
- Create: `src/personal_organizer/providers/embedding/__init__.py` (empty)
- Create: `src/personal_organizer/providers/embedding/<vendor>.py`
- Modify: `src/personal_organizer/core/errors.py` (before `__all__`, about line 190)
- Modify: `src/personal_organizer/settings.py` (`ModelSettings` line 144; new class after `VoiceSettings`; `Settings` field; `__all__`)
- Modify: `.env.example:31`
- Test: `tests/unit/test_<vendor>_embedder.py`, `tests/unit/test_settings.py`

**Interfaces:**
- Consumes: the "Decided by stage 1" row for the winner.
- Produces, for #79:
  - `interfaces.embedding.EmbeddingProvider`, a `runtime_checkable` Protocol: `async def embed(self, texts: Sequence[str], *, kind: EmbeddingKind) -> list[list[float]]`, where `EmbeddingKind = Literal["document", "query"]`
  - `core.errors.EmbeddingError(AppError)` with subclasses `EmbeddingUnavailableError` and `EmbeddingRejectedError`, codes `embedding_error`, `embedding_unavailable`, `embedding_rejected`
  - `<Vendor>Embedder.from_settings(settings, *, transport: httpx.AsyncBaseTransport | None = None)`
  - `settings.embedding.api_key`, `settings.embedding.request_timeout_s`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_settings.py`, after `TestVoice`:

```python
class TestEmbedding:
    def test_no_key_by_default(self, settings: Settings) -> None:
        assert settings.embedding.api_key is None

    def test_the_key_is_read_and_hidden(self, settings_factory: Any) -> None:
        cfg = settings_factory(EMBEDDING__API_KEY="embed-test-key")
        assert cfg.embedding.api_key is not None
        assert "embed-test-key" not in repr(cfg) + cfg.model_dump_json()

    def test_the_default_dimension_is_1024(self, settings: Settings) -> None:
        assert settings.models.embedding_dimensions == 1024
```

`tests/unit/test_<vendor>_embedder.py`, in the shape of `test_soniox_transcriber.py`. `_embedder(settings_factory, handler, **env)` builds it with `EMBEDDING__API_KEY="embed-test-key"`, `MODELS__EMBEDDING_MODEL="<winner>"` and `httpx.MockTransport(handler)`. `_ok(vectors, seen)` answers in the winner's response shape with `tokens=3`. `TEXT = "תזכיר לי מחר בשש"`, `VECTOR = [0.1] * 1024`. `PATH`, `DOCUMENT_WIRE` and `QUERY_WIRE` are the winner's path and two `input_type` values, and `expected_body(texts, wire)` is its exact request body. Annotate the tests as `test_soniox_transcriber.py` does, since mypy strict covers `tests/`.

```python
@pytest.mark.parametrize(("kind", "wire"), [("document", DOCUMENT_WIRE), ("query", QUERY_WIRE)])
async def test_it_sends_the_model_kind_and_dimension(settings_factory, kind, wire):
    seen = []
    embedder = _embedder(settings_factory, _ok([VECTOR], seen))
    assert isinstance(embedder, EmbeddingProvider)
    assert await embedder.embed([TEXT], kind=kind) == [VECTOR]
    assert seen[0].url.path == PATH
    assert seen[0].headers["authorization"] == "Bearer embed-test-key"
    assert json.loads(seen[0].read()) == expected_body([TEXT], wire)  # exact, per the winner's row

async def test_an_empty_list_makes_no_call(settings_factory):
    seen = []
    assert await _embedder(settings_factory, _ok([], seen)).embed([], kind="query") == []
    assert seen == []

@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_statuses(settings_factory, status):
    embedder = _embedder(settings_factory, lambda r: httpx.Response(status, json={}))
    with pytest.raises(EmbeddingUnavailableError):
        await embedder.embed([TEXT], kind="query")

@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
async def test_refusals(settings_factory, status):
    embedder = _embedder(settings_factory, lambda r: httpx.Response(status, json={}))
    with pytest.raises(EmbeddingRejectedError):
        await embedder.embed([TEXT], kind="query")

async def test_unreachable_is_unavailable_and_tried_once(settings_factory):
    tries = []
    def boom(request):
        tries.append(request)
        raise httpx.ConnectError("down", request=request)
    with pytest.raises(EmbeddingUnavailableError) as excinfo:
        await _embedder(settings_factory, boom).embed([TEXT], kind="query")
    assert len(tries) == 1  # the job layer is the only retry
    assert excinfo.value.__cause__ is None

@pytest.mark.parametrize("vectors", [[], [[0.1] * 3], [VECTOR, VECTOR]])
async def test_the_wrong_count_or_length_is_refused(settings_factory, vectors):
    with pytest.raises(EmbeddingRejectedError):
        await _embedder(settings_factory, _ok(vectors, [])).embed([TEXT], kind="query")

async def test_an_unusable_body_is_refused(settings_factory):
    bad = [httpx.Response(200, text="not json"), httpx.Response(200, json={})]
    for reply in bad:
        with pytest.raises(EmbeddingRejectedError):
            await _embedder(settings_factory, lambda r: reply).embed([TEXT], kind="query")

async def test_the_text_stays_out_of_errors_and_logs(settings_factory):
    def echo(request):
        return httpx.Response(400, json={"detail": TEXT, "message": TEXT})
    with capture_logs() as logs, pytest.raises(EmbeddingRejectedError) as excinfo:
        await _embedder(settings_factory, echo).embed([TEXT], kind="document")
    assert TEXT not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert TEXT not in repr(logs)

async def test_a_success_logs_counts_only(settings_factory):
    with capture_logs() as logs:
        await _embedder(settings_factory, _ok([VECTOR], [])).embed([TEXT], kind="document")
    assert logs[0]["event"] == "embedding.completed"
    assert (logs[0]["texts"], logs[0]["tokens"]) == (1, 3)
    assert TEXT not in repr(logs)
    assert "embed-test-key" not in repr(logs)

def test_no_key_is_a_config_error(settings_factory):
    with pytest.raises(ConfigError):
        <Vendor>Embedder.from_settings(settings_factory())
```

Voyage only:

```python
async def test_vectors_line_up_with_their_texts_when_out_of_order(settings_factory):
    first, second = [0.1] * 1024, [0.2] * 1024
    body = {"data": [{"index": 1, "embedding": second}, {"index": 0, "embedding": first}],
            "usage": {"total_tokens": 3}}
    embedder = _embedder(settings_factory, lambda r: httpx.Response(200, json=body))
    assert await embedder.embed(["a", "b"], kind="document") == [first, second]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src uv run pytest tests/unit/test_settings.py::TestEmbedding tests/unit/test_<vendor>_embedder.py`
Expected: FAIL (`ImportError` on the embedder module; no `embedding` on `Settings`).

- [ ] **Step 3: Add the settings, errors and interface**

- `settings.py`:
  - `embedding_dimensions: int = 1024`.
  - New `class EmbeddingSettings(BaseModel)` with `api_key: SecretStr | None = None` and `request_timeout_s: float = 15.0`.
  - `Settings.embedding: EmbeddingSettings = EmbeddingSettings()`, and add it to `__all__`.
  - No `enabled` flag: nothing calls this yet, and a missing key is a `ConfigError` in `from_settings`. #79 owns when skills turn on.
- `core/errors.py`: the three classes, messages fixed strings, in the shape of the `VoiceError` family. Add them to `__all__`. Separate from `LLMError`, because `conversation.py:105` and the agent's handlers catch those to send a person fixed text, which is the wrong response to a failed search.
- `interfaces/embedding.py`: the Protocol. A one-paragraph docstring says vendors embed questions and stored text differently, which is why `kind` is required. It imports nothing outside `typing` and `collections.abc`.
- `.env.example:31`: set the winner's model ID, then add below it:
  ```
  # 1024 by default, and fixed by the vector(N) column: changing it is a migration and a re-embed.
  # MODELS__EMBEDDING_DIMENSIONS=1024
  # EMBEDDING__API_KEY=
  ```
  Leave the `text-embedding-3-small` placeholders in `tests/conftest.py` and `ci.yml`; nothing reads them.

- [ ] **Step 4: Implement the provider**

Same layout as `soniox.py`: `BASE_URL`, `_TRANSIENT_STATUSES = frozenset({408, 429})`, `_translate(exc: httpx.HTTPError)`, and `from_settings` raising `ConfigError("<Vendor>Embedder needs EMBEDDING__API_KEY")`. The constructor is `(client, *, model: str, dimensions: int)`.

`embed`:

1. Return `[]` for no texts.
2. Make one POST and `raise_for_status`.
3. Parse per the table.
4. `httpx.HTTPError` goes to `_translate(...) from None`; 5xx, the two transient statuses and transport errors are unavailable, other 4xx are rejected. `KeyError`, `TypeError` and `ValueError` raise `EmbeddingRejectedError("embedding response unusable") from None`.
5. If the count differs from `len(texts)` or any vector's length differs from `dimensions`, raise that same error.
6. `log.info("embedding.completed", model=..., texts=len(texts), tokens=...)`.

No `observation(...)` wrapper, because it would send the text to Langfuse. No truncation field: the vendor default is fine for short text. Add `# shortcut: one request, so over <limit> texts is a 400; split when a backfill needs it.`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src uv run pytest tests/unit/test_settings.py tests/unit/test_<vendor>_embedder.py`
Expected: PASS.

- [ ] **Step 6: Smoke-check against the real API**

Mock tests can't catch a wrong field name. Save as `/tmp/smoke_78.py`, with the winner's key name and model:

```python
import asyncio, os, sys

os.environ["EMBEDDING__API_KEY"] = os.environ["<VENDOR>_API_KEY"]
os.environ["MODELS__EMBEDDING_MODEL"] = "<winner model>"
from personal_organizer.providers.embedding.<vendor> import <Vendor>Embedder
from personal_organizer.settings import Settings

vectors = asyncio.run(<Vendor>Embedder.from_settings(Settings()).embed(["תור לרופא", "a doctor's appointment"], kind="document"))
sys.stdout.write(f"{len(vectors)} {len(vectors[0])}\n")
```

Run: `PYTHONPATH=src uv run --env-file .env python /tmp/smoke_78.py`
Expected: `2 1024`

- [ ] **Step 7: Full checks**

Run: `PYTHONPATH=src uv run pytest`
Run: `uv run ruff check .`
Run: `uv run ruff format --check .`
Run: `uv run mypy`
Run: `uv run --env-file .env python /tmp/scan_78.py`
Expected: all pass; the scan is silent.

- [ ] **Step 8: Commit**

Stage each file by name: the `git add` list is the **Files** block above, with `<vendor>` filled in.

```bash
git commit -m "feat(embeddings): hosted embedding provider behind a small interface (#78)"
```

- [ ] **Step 9: Update the graph**

Run: `PYTHONHASHSEED=0 graphify update .`
Do not add `graphify-out/`.

## Finish

Per the roadmap: draft PR for #78, marked ready after CI. The PR body lists:

- The ADR's result.
- **Before #79 uses this:** turn on the training opt-out on the winner's account; on staging's worker set `EMBEDDING__API_KEY`, and set `MODELS__EMBEDDING_MODEL` to the winner. If the winner is Cohere, use a production key.
- **For #79:** add `EmbeddingUnavailableError` to the retry set of whichever job calls it (`INBOUND_RETRY` in `worker/tasks/channel.py`, or its engine's equivalent), and create the column as `vector(1024)`.
- No staging check: nothing calls the provider yet, and the roadmap lists none for #78.
