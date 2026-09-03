"""The "one brain": a Perceiver-style per-day cross-ticker market encoder.

Full attention across ~6,000 tickers x 128-day windows is intractable, so
the whole-market mechanism is a bottleneck: each calendar day, a small set
of learned latent "market tokens" cross-attends to EVERY eligible ticker's
embedded day-t features (cost ~ n_tickers x n_latents x d -- trivial even at
full-universe scale), then one self-attention layer mixes the latents.
Output per day: the latent tokens (kept for the market auxiliary head) and
a pooled market embedding m_t (the day's market state) that the temporal
model consumes at every window position.

Cross-ticker attention only ever sees day-<=t inputs, so this direction of
learning ("sideways across the market") is leakage-free at prediction time
by construction. Attention over a SET of tickers is permutation-invariant --
tests/test_octopus_model.py asserts that reordering the tickers of a day
changes nothing.

torch is imported at module top here because this module is only ever
imported from octopus_model.py's lazy paths -- importing the package without
torch stays possible as long as nothing imports THIS module.
"""

from __future__ import annotations

import torch  # type: ignore
from torch import nn  # type: ignore


class MarketDayEncoder(nn.Module):
    def __init__(
        self,
        *,
        n_features: int,
        d_model: int,
        n_latents: int = 16,
        n_heads: int = 4,
        ticker_embedding: nn.Embedding | None = None,
        vocab_size: int = 1,
    ) -> None:
        super().__init__()
        self.feat_proj = nn.Linear(n_features, d_model)
        # Shared with the temporal model when provided: one identity per
        # ticker, seen the same way by both the arms and the brain.
        self.ticker_embedding = (
            ticker_embedding if ticker_embedding is not None else nn.Embedding(vocab_size, d_model)
        )
        self.latents = nn.Parameter(torch.randn(n_latents, d_model) * 0.02)
        self.cross_attention = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
        self.latent_mixer = nn.TransformerEncoderLayer(
            d_model, n_heads, dim_feedforward=2 * d_model, batch_first=True, dropout=0.0
        )

    def forward(self, day_features: torch.Tensor, ticker_ids: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """One day's market state.

        day_features: (n_tickers_today, n_features) normalized floats;
        ticker_ids: (n_tickers_today,) vocabulary ids (0 = unknown).
        Returns (latent_tokens (n_latents, d_model), pooled m_t (d_model,)).
        """
        tokens = self.feat_proj(day_features) + self.ticker_embedding(ticker_ids)
        latents = self.latents.unsqueeze(0)  # (1, M, d)
        attended, _ = self.cross_attention(latents, tokens.unsqueeze(0), tokens.unsqueeze(0))
        mixed = self.latent_mixer(attended).squeeze(0)  # (M, d)
        return mixed, mixed.mean(dim=0)
