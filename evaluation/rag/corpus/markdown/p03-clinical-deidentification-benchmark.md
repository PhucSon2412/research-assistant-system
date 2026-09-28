# CNDI-5k: A Benchmark for Clinical Note De-identification

Authors: Priya Raman and Tomasz Wolak (fictional authors; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p03 (fictional, not registered).

## Abstract

CNDI-5k contains 5,000 synthetic clinical notes annotated for 11 categories of protected health information.
We release baselines for span-level de-identification.

## Annotation

Three annotators labelled every note, and inter-annotator agreement reached a Cohen's kappa of 0.87.
Notes were generated from templates and contain no real patient data.
Disagreements were resolved by a fourth senior annotator.

## Results

A hybrid model combining a conditional random field with a BERT encoder reached a span-level F1 of 0.912.
A rule-based baseline reached a span-level F1 of 0.744.
Dates and phone numbers were the easiest categories, while free-text occupations were the hardest at F1 0.61.

## Access

The dataset is released under a research-only licence that forbids re-identification attempts.
Access requires a signed data use agreement.

## Limitations

All notes are in English, and template generation may understate the variety of real clinical writing.

## References

[1] K. Osei. Template Generation of Synthetic Health Records. 2024.
