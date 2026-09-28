import pytest
import torch
from torch import nn

from market_encoder import MarketDayEncoder


def test_market_encoder_shape():
    """Verify output shapes of the market encoder are correct."""
    d_model = 32
    n_features = 10
    n_latents = 8
    n_tickers = 100
    vocab_size = 50

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )

    day_features = torch.randn(n_tickers, n_features)
    ticker_ids = torch.randint(0, vocab_size, (n_tickers,))

    mixed, m_t = encoder(day_features, ticker_ids)

    assert mixed.shape == (n_latents, d_model), "Latents shape is incorrect"
    assert m_t.shape == (d_model,), "Pooled market embedding shape is incorrect"


def test_permutation_invariance():
    """Verify that reordering the input tickers does not change the outputs (up to numerical precision)."""
    d_model = 32
    n_features = 10
    n_latents = 8
    n_tickers = 50
    vocab_size = 100

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )
    encoder.eval()

    day_features = torch.randn(n_tickers, n_features)
    ticker_ids = torch.randint(0, vocab_size, (n_tickers,))

    mixed1, m_t1 = encoder(day_features, ticker_ids)

    perm = torch.randperm(n_tickers)
    day_features_shuffled = day_features[perm]
    ticker_ids_shuffled = ticker_ids[perm]

    mixed2, m_t2 = encoder(day_features_shuffled, ticker_ids_shuffled)

    assert torch.allclose(mixed1, mixed2, atol=1e-5), "Outputs changed after permuting tickers!"
    assert torch.allclose(m_t1, m_t2, atol=1e-5), "Pooled embedding changed after permuting tickers!"


def test_ticker_embedding_sharing():
    """Verify the encoder uses an external ticker_embedding if provided."""
    d_model = 32
    n_features = 10
    vocab_size = 50

    shared_emb = nn.Embedding(vocab_size, d_model)

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        ticker_embedding=shared_emb,
        vocab_size=vocab_size,
    )

    assert encoder.ticker_embedding is shared_emb


def test_empty_zero_length_inputs():
    """Verify behavior when the number of tickers is zero."""
    d_model = 32
    n_features = 10
    n_latents = 8
    vocab_size = 50

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )

    day_features = torch.randn(0, n_features)
    ticker_ids = torch.randint(0, vocab_size, (0,))

    # In modern PyTorch, MultiheadAttention *can* handle sequence length 0
    # without raising an error. It outputs zero/nan tensors or passes through cleanly.
    # We should assert that it returns valid shapes.
    mixed, m_t = encoder(day_features, ticker_ids)

    assert mixed.shape == (n_latents, d_model), "Latents shape is incorrect for zero-length inputs"
    assert m_t.shape == (d_model,), "Pooled market embedding shape is incorrect for zero-length inputs"


def test_malformed_dimensions():
    """Verify that passing inputs with incorrect dimensions raises an error."""
    d_model = 32
    n_features = 10
    n_latents = 8
    n_tickers = 5
    vocab_size = 50

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )

    # Note: `nn.Linear` (self.feat_proj) supports any number of dimensions, broadcasting over the last dimension.
    # However, `ticker_embedding` (nn.Embedding) only expects 1D or ND input.
    # BUT `tokens` is `day_features + ticker_ids_embedding`.
    # Let's test a dimension mismatch between day_features and ticker_ids!
    day_features_mismatch = torch.randn(n_tickers, n_features)
    ticker_ids_mismatch = torch.randint(0, vocab_size, (n_tickers + 1,))

    with pytest.raises(RuntimeError, match="The size of tensor a.*"):
        # The sum `tokens = self.feat_proj(day_features) + self.ticker_embedding(ticker_ids)` will fail.
        encoder(day_features_mismatch, ticker_ids_mismatch)

    # Extra dimension for day_features, but not ticker_ids, causes issues in attention layer due to 4D vs 3D shapes
    day_features_3d = torch.randn(2, n_tickers, n_features)
    ticker_ids_1d = torch.randint(0, vocab_size, (n_tickers,))
    with pytest.raises(AssertionError):
        encoder(day_features_3d, ticker_ids_1d)


def test_nan_inf_values():
    """Verify that NaN/Inf inputs result in NaN/Inf outputs."""
    d_model = 32
    n_features = 10
    n_latents = 8
    n_tickers = 5
    vocab_size = 50

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )

    day_features = torch.randn(n_tickers, n_features)
    # Inject NaN
    day_features[0, 0] = float("nan")
    ticker_ids = torch.randint(0, vocab_size, (n_tickers,))

    mixed, m_t = encoder(day_features, ticker_ids)

    # We expect output to contain NaNs because it propagated through attention
    assert torch.isnan(mixed).any(), "Expected NaNs to propagate to mixed latents"
    assert torch.isnan(m_t).any(), "Expected NaNs to propagate to pooled embedding"

    # Inject Inf
    day_features = torch.randn(n_tickers, n_features)
    day_features[0, 0] = float("inf")

    mixed, m_t = encoder(day_features, ticker_ids)

    # Similarly, expected Infs or NaNs to propagate
    assert torch.isnan(mixed).any() or torch.isinf(mixed).any(), "Expected Infs/NaNs to propagate"
    assert torch.isnan(m_t).any() or torch.isinf(m_t).any(), "Expected Infs/NaNs to propagate"


def test_out_of_bounds_ticker_ids():
    """Verify that using a ticker ID out of the vocabulary size raises an IndexError."""
    d_model = 32
    n_features = 10
    n_latents = 8
    n_tickers = 5
    vocab_size = 50

    encoder = MarketDayEncoder(
        n_features=n_features,
        d_model=d_model,
        n_latents=n_latents,
        vocab_size=vocab_size,
    )

    day_features = torch.randn(n_tickers, n_features)
    # Ticker ID out of bounds
    ticker_ids = torch.tensor([0, 1, vocab_size + 10, 2, 3])

    with pytest.raises(IndexError):
        encoder(day_features, ticker_ids)
