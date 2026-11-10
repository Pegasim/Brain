# Evaluation plan

No results are claimed. Agree on the test set, scoring rules and target before seeing results. Keep real test material private and outside git. Report sample size, time window, source coverage, uncertainty and failure examples; avoid treating the two founders as a representative population.

## Memory / context: first proposed experiment
Create a set of real, answerable questions with source-backed expected answers across at least two sources. Include questions with no answer, conflicting records and stale facts. Split development and held-out questions by event or time to prevent leakage. Compare (a) general model without personal data, (b) existing personal workflow, and (c) proposed retrieval system, using comparable model and prompt where possible. Score factual correctness, source support, appropriate abstention and time to answer. Measure whether added context improves outcomes, not merely how much is retrieved. Set a success threshold together before testing.

## Representation: separate experiment
Have the owner privately record responses to situations or hold out historical replies. Compare the proposed system with a general model and simple examples-only baseline. Blind the scorer where practical; assess meaning and choices separately from writing style. Do not treat similarity in tone as proof of identity or autonomous authority. Avoid showing future or held-out material during generation.

## Forecasting: separate experiment
Define a specific target and horizon (e.g., next-day steps, if available). Use chronological walk-forward evaluation with only data available at each cutoff. Compare against last observation and same-weekday averages; include additional simple baselines as appropriate. Report absolute error, baseline-relative error, sample counts, missingness and per-period performance. Do not choose the winning target after testing without marking it exploratory. Forecasting activity does not validate a conversational twin.

## Next decision
Agree which experiment to prioritize, the smallest viable source set, a minimum number of held-out cases and a predeclared threshold. Failed results are results too.
