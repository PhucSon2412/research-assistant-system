# Revisiting Gated Sparse Routing: A Replication Study

Authors: Tove Lindqvist (fictional author; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p04 (fictional, not registered).

## Abstract

We replicate Gated Sparse Routing on Kinyarwanda–English using the published configuration.
We observe a gain of 0.6 BLEU over the dense baseline, much smaller than the 2.4 points originally reported.

## Methods

We used sixteen experts with top-2 routing and a load-balancing coefficient of 0.01, matching the original description.
Unlike the original study, we trained both models with one unigram tokenizer of 32,000 pieces learned on the combined source and target corpus.
Both models were trained for the same number of updates on the 1.2 million sentence pairs.

## Results

The dense baseline scored 26.8 BLEU and the routed model scored 27.4 BLEU.
When we re-ran the baseline with the tokenizer settings reported for the original baseline, it dropped to 25.6 BLEU.

## Discussion

We attribute most of the original gap to a tokenizer mismatch between the baseline and the routed model rather than to routing itself.
We did not have access to the original checkpoints, so other differences cannot be ruled out.

## References

[1] L. Okafor and M. Reyes. Gated Sparse Routing for Low-Resource Machine Translation. 2026.
