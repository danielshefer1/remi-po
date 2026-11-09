# ADR 0007: Hosted embeddings

Status: Accepted: `voyage-4`, chosen by the owner on 2026-11-08 after the winner rule tied (see
Decision).

## Context

Matching a request to a saved skill needs embeddings (#78). Notes stay all-in-prompt and are
out of scope. Running Qwen3-Embedding-0.6B ourselves measured weak Hebrew recall and costs
about $24 a month for the RAM it needs (#78, 2026-10-17). A hosted model costs cents at the circle's size. This bake-off reruns the
original 20-note, 20-question set (the one Qwen was tested on, recovered from its transcript
and kept as `scripts/embedding_bakeoff_set.json`, which `scripts/embedding_bakeoff.py` runs), so Qwen's numbers are directly comparable
without a control run. The set is invented, so sending it to both vendors exposed no one's
words.

## What ran

Four models at 1024 dimensions, three conditions (all English, all Hebrew, mixed as people
type), each asked for the top 3 notes out of 20. The hosted models take an `input_type`
(query or document) where Qwen used an instruction prefix. `embed-multilingual-v3.0` is fixed
at 1024 and was sent no dimension. `voyage-4-lite` and `embed-v5.0-fast` were left out
beforehand.

## Results

hit@3, with hit@1 in brackets. Tokens are for the whole run.

| model | tokens | english | hebrew | mixed | mixed same-lang | mixed Hebrew-English |
|---|---|---|---|---|---|---|
| Qwen3-Embedding-0.6B (earlier run) | n/a | 20/20 (18) | 13/20 (9) | 13/20 (11) | 12/13 | 1/7 |
| voyage-4-large | 1057 | 19/20 (18) | 19/20 (15) | 20/20 (18) | 13/13 (12) | 7/7 (6) |
| voyage-4 | 1057 | 19/20 (17) | 19/20 (18) | 20/20 (17) | 13/13 (12) | 7/7 (5) |
| embed-v5.0-pro | 1115 | 20/20 (17) | 20/20 (17) | 20/20 (17) | 13/13 (12) | 7/7 (5) |
| embed-multilingual-v3.0 | 1059 | 20/20 (18) | 20/20 (19) | 18/20 (17) | 13/13 (13) | 5/7 (4) |

Misses at rank 1 (question index: wanted note, top note), as the script printed them.

- voyage-4-large: english q13 (13, 17), q17 (17, 18). hebrew q0 (0, 15), q1 (0, 17), q3 (1, 8),
  q13 (13, 17), q14 (14, 6). mixed q4 (3, 8), q13 (13, 17).
- voyage-4: english q4 (3, 8), q13 (13, 0), q14 (14, 6). hebrew q0 (0, 15), q1 (0, 15).
  mixed q4 (3, 8), q5 (4, 14), q14 (14, 6).
- embed-v5.0-pro: english q1 (0, 18), q13 (13, 0), q17 (17, 1). hebrew q4 (3, 16), q7 (6, 14),
  q14 (14, 6). mixed q0 (0, 15), q1 (0, 18), q11 (10, 1).
- embed-multilingual-v3.0: english q3 (1, 18), q13 (13, 0). hebrew q13 (13, 0). mixed q0 (0, 13),
  q5 (4, 11), q11 (10, 1).

## Decision

The rule was fixed before the numbers and is applied unchanged.

Gates (English >= 19/20, Hebrew >= 17/20, same-language >= 12/13, Hebrew-English >= 5/7):
all four models pass. Score (Hebrew hits + Hebrew-English hits, Qwen 14): embed-v5.0-pro 27,
voyage-4-large 26, voyage-4 26, embed-multilingual-v3.0 25.

1. No model qualifies: no, all four qualify. Continue.
2. One qualifies: no, four do. Continue.
3. Highest score leads the next by 2 or more: no. embed-v5.0-pro (27) leads by 1. Continue.
4. Tied within 1 of the top: embed-v5.0-pro (27), voyage-4-large (26), voyage-4 (26);
   embed-multilingual-v3.0 (25) is 2 behind and out. More hit@1 across all-Hebrew plus mixed,
   needing a lead of 2: voyage-4 35, embed-v5.0-pro 34, voyage-4-large 33. voyage-4 leads
   by 1. Still tied.
5. A list price at least 25% lower, from the vendor's own page: voyage-4 ($0.06/M) is 50%
   lower than voyage-4-large ($0.12/M), both on Voyage's pricing page. No embed-v5.0-pro
   price was found on cohere.com/pricing (one tab of that page, "Advanced retrieval models",
   did not load), so the rule could not compare it. Only third parties list it, at $0.12/M,
   and the rule bars those. Not resolvable.
6. Still tied: STOP, with a recommendation.

Outcome: the rule stopped at step 6. The recommendation put to the owner was `voyage-4`, and
the owner accepted it on 2026-11-08.

The recommendation's grounds: quality was a tie within the rule's noise (`voyage-4` has the
best hit@1 of the tied group and passes every gate); zero-day retention after opting out,
against Cohere's 30-day logs; and a price published on the vendor's own page, half that of
`voyage-4-large`. Two caveats: `embed-multilingual-v3.0` has the best raw hit@1 for
Hebrew (19) but is weakest on Hebrew-English (5/7) and was out by the rule at step 4; and a Voyage opt-out needs a payment method (see Data use).

Dimension: 1024. It is common to all four candidates and under
the pgvector HNSW limit; storage at this scale is immaterial, so smaller sizes were not tested.

## Cost

Assumptions: 50 people x 25 texts each x 200 tokens = 250,000 tokens, embedded once. At
$0.12/M that is about $0.03; at $0.06/M (voyage-4) about $0.015. Query volume is unmeasured, but even at the same volume every month the cost stays a few cents a month. Against
Qwen's about $24 a month for hosting. Voyage's first 200M tokens are free, which this
volume would not exhaust.

Prices, from the vendors' own pages: Voyage, `voyage-4-large` $0.12/M, `voyage-4` $0.06/M,
200M free tokens (https://docs.voyageai.com/docs/pricing). Cohere: no Embed price on
https://cohere.com/pricing (one tab of that page did not load). A third-party figure of $0.12/M for `embed-v5.0-pro` exists
(aggregators such as cloudprice.net, getmaxim.ai); it is third-party and was not used by the
rule.

## Data use

Voyage, direct API (`api.voyageai.com`): the Voyage terms of service (https://www.voyageai.com/tos)
license Customer Content to Voyage "to train, improve, and otherwise further develop the
Service" unless the customer opts out. The opt-out is a toggle under Terms of Service in the
dashboard; it needs a payment method on file; it applies only to content sent after it is set,
and content sent earlier may still be used; opting out may void free tokens at Voyage's
discretion. After opting out, content is deleted right after processing, which the FAQ calls
zero-day retention. Opting out cannot be undone from the dashboard, and it needs an organization
Admin. For anyone not opted out, the licence is perpetual and no deletion period is given. The
terms say "Customer Content" and do not name the API explicitly.
The MongoDB page (https://www.mongodb.com/docs/voyageai/management/model-training-data/)
covers only the Atlas API and is not relevant to `api.voyageai.com`.

Cohere: the opt-out is under Settings, then Data Controls in the dashboard; logged prompts and
generations are deleted after 30 days, with exceptions; the commitments cover accounts with a
card on file, and trial users fall under the general Terms and Privacy Policy
(https://cohere.com/enterprise-data-commitments). Trial keys are capped at 1,000 API calls a
month (https://docs.cohere.com/docs/rate-limits) and may not be used for production or
commercial purposes (https://cohere.com/pricing). None of the pages read states the default for training in
plain words, though the opt-out wording implies it is on until opted out.
`docs.cohere.com/docs/data-retention-and-opt-out` returned 404. Only part of
https://cohere.com/terms-of-use was read.

Plainly: the opt-out must be set on the Voyage account, with a card on file, before any real
text goes there. It does nothing for text sent before it is set. So far only the invented test
set has been sent.

## Limits

One test set of 20 short notes, not skills; a gap of 1 hit in 27 is noise, and hit@1
differences of 1 or 2 are weak evidence. `embed-multilingual-v3.0`
truncates past 512 tokens. Cohere's Embed pricing could not be confirmed from Cohere's own
pages on 2026-11-08.
