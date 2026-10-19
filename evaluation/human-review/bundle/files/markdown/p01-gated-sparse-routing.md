# Gated Sparse Routing for Low-Resource Machine Translation

Authors: Lena Okafor and Mateo Reyes (fictional authors; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p01 (fictional, not registered).

## Abstract

We introduce Gated Sparse Routing (GSR), a mixture-of-experts layer that sends each token to a small subset of experts through a learned gate.
On the Kinyarwanda–English benchmark, GSR improves BLEU by 2.4 points over a dense Transformer baseline with the same number of active parameters.

## Introduction

Low-resource translation models must share capacity between frequent and rare morphology.
Dense models apply every parameter to every token, which wastes capacity on common words.
Sparse routing lets rare word forms use specialised experts without increasing the cost per token.

## Methods

Each GSR layer contains sixteen feed-forward experts.
The gate selects the top-2 experts per token and adds a load-balancing loss with coefficient 0.01.
We replace every second feed-forward block of a 6-layer encoder-decoder with a GSR layer.
Training used 1.2 million Kinyarwanda–English sentence pairs.
Tokenization used a SentencePiece vocabulary of 32,000 pieces trained on the target-side corpus only for the dense baseline and on both sides for GSR.

## Results

GSR reached 27.9 BLEU on the Kinyarwanda–English test set, compared with 25.5 BLEU for the dense baseline.
On Luganda–English the gain over the dense baseline was 1.3 BLEU points.
Training took 38 GPU-hours for GSR, against 29 GPU-hours for the dense baseline.

## Analysis

Expert utilisation stayed within 4% of uniform after the first 5,000 updates.
Removing the load-balancing loss caused two experts to receive 61% of all tokens and reduced BLEU by 0.9 points.

## Limitations

GSR lost 1.8 BLEU on code-switched Kinyarwanda–English input, where the gate routed most mixed-language tokens to a single expert.
We did not evaluate languages outside the Bantu family.

## Conclusion

Gated Sparse Routing is a simple way to add capacity for rare morphology in low-resource translation.

## References

[1] H. Mensah. Dense Adapter Stacks for Low-Resource Translation. 2025.
[2] S. Achterberg. Load Balancing Losses for Sparse Experts. 2024.
