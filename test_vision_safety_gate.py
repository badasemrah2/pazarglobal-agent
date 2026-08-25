"""
Vision Safety Gate Tests - Fail-Closed Behavior

Bu testler, vision güvenlik katmanının fail-CLOSED davranışını doğrular:
- API hatası veya timeout durumunda içerik ENGELLENIR (fail-open değil)
- Boş moderation response → ENGELLENİR
- Silah/illegal içerik → ENGELLENİR
- Moderation yapılandırılmamışsa → ENGELLENİR
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agents.vision_safety_gate import VisionSafetyGate


@pytest.fixture
def safety_gate():
    gate = VisionSafetyGate.__new__(VisionSafetyGate)
    gate.client = MagicMock()  # Simulate initialized client
    return gate


@pytest.fixture
def safety_gate_no_client():
    gate = VisionSafetyGate.__new__(VisionSafetyGate)
    gate.client = None  # Simulate missing API key
    return gate


# ─────────────────────────────────────────────
# FAIL-CLOSED: API yapılandırılmamış
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_client_blocks_all_media(safety_gate_no_client):
    """OpenAI client yoksa tüm medya ENGELLENMELI (fail-closed)"""
    result = await safety_gate_no_client.check_media(["https://example.com/image.jpg"])
    assert result["safe"] is False
    assert result["allow_listing"] is False
    assert "moderation_not_configured" in result["flagged_categories"]
    assert result.get("block_reason") is not None


@pytest.mark.asyncio
async def test_empty_media_always_passes(safety_gate_no_client):
    """Boş medya listesi her zaman geçmeli (upload yoksa kontrol gerekmez)"""
    result = await safety_gate_no_client.check_media([])
    assert result["safe"] is True
    assert result["allow_listing"] is True


# ─────────────────────────────────────────────
# FAIL-CLOSED: API hatası / timeout
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_moderation_api_exception_blocks_content(safety_gate):
    """Moderation API exception fırlatırsa içerik ENGELLENMELİ (fail-closed)"""
    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(
        side_effect=Exception("Connection timeout")
    )

    result = await safety_gate.check_media(["https://example.com/image.jpg"])
    assert result["safe"] is False
    assert result["allow_listing"] is False
    assert result.get("block_reason") is not None


@pytest.mark.asyncio
async def test_moderation_empty_response_blocks_content(safety_gate):
    """Moderation API boş sonuç dönerse içerik ENGELLENMELİ (fail-closed)"""
    mock_response = MagicMock()
    mock_response.results = []  # Boş liste
    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(return_value=mock_response)

    result = await safety_gate.check_media(["https://example.com/image.jpg"])
    assert result["safe"] is False
    assert result["allow_listing"] is False


@pytest.mark.asyncio
async def test_single_image_exception_blocks(safety_gate):
    """Tek görselde exception olursa o görsel ENGELLENMELİ (fail-closed)"""
    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(
        side_effect=Exception("Rate limit exceeded")
    )

    result = await safety_gate._check_single_image("https://example.com/gun.jpg")
    assert result["safe"] is False
    assert "moderation_error" in result.get("categories", [])


# ─────────────────────────────────────────────
# Tehlikeli içerik → ENGELLEME
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weapon_image_blocked(safety_gate):
    """Silah görseli ENGELLENMELİ"""
    cats = MagicMock()
    cats.sexual = False
    cats.violence = False
    cats.hate = False
    cats.harassment = False
    cats.self_harm = False
    cats.illicit = True  # Silah = illicit

    mock_result = MagicMock()
    mock_result.categories = cats
    mock_result.category_scores = MagicMock(sexual=0.0, violence=0.0, hate=0.0)

    mock_response = MagicMock()
    mock_response.results = [mock_result]

    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(return_value=mock_response)

    result = await safety_gate._check_single_image("https://example.com/gun.jpg")
    assert result["safe"] is False
    assert "illicit" in result["categories"]


@pytest.mark.asyncio
async def test_violence_image_blocked(safety_gate):
    """Şiddet içerikli görsel ENGELLENMELİ"""
    cats = MagicMock()
    cats.sexual = False
    cats.violence = True
    cats.hate = False
    cats.harassment = False
    cats.self_harm = False
    cats.illicit = False

    mock_result = MagicMock()
    mock_result.categories = cats
    mock_result.category_scores = MagicMock(sexual=0.0, violence=0.9, hate=0.0)

    mock_response = MagicMock()
    mock_response.results = [mock_result]

    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(return_value=mock_response)

    result = await safety_gate._check_single_image("https://example.com/violence.jpg")
    assert result["safe"] is False
    assert "violence" in result["categories"]


@pytest.mark.asyncio
async def test_safe_product_image_passes(safety_gate):
    """Güvenli ürün görseli GEÇMELİ"""
    cats = MagicMock()
    cats.sexual = False
    cats.violence = False
    cats.hate = False
    cats.harassment = False
    cats.self_harm = False
    cats.illicit = False

    mock_result = MagicMock()
    mock_result.categories = cats
    mock_result.category_scores = MagicMock(sexual=0.0, violence=0.0, hate=0.0)

    mock_response = MagicMock()
    mock_response.results = [mock_result]

    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(return_value=mock_response)

    result = await safety_gate._check_single_image("https://example.com/fridge.jpg")
    assert result["safe"] is True
    assert result["categories"] == []


@pytest.mark.asyncio
async def test_sexual_only_apparel_exception_passes(safety_gate):
    """Sadece sexual flag varsa ve ürün istisnasına uygunsa görsel geçmeli."""
    cats = MagicMock()
    cats.sexual = True
    cats.sexual_minors = False
    cats.violence = False
    cats.hate = False
    cats.harassment = False
    cats.self_harm = False
    cats.illicit = False

    mock_result = MagicMock()
    mock_result.categories = cats
    mock_result.category_scores = MagicMock(sexual=0.8, violence=0.0, hate=0.0)

    mock_response = MagicMock()
    mock_response.results = [mock_result]

    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(return_value=mock_response)
    safety_gate._is_allowed_apparel_exception = AsyncMock(return_value=True)

    result = await safety_gate._check_single_image("https://example.com/bikini.jpg")
    assert result["safe"] is True
    assert result["categories"] == []


@pytest.mark.asyncio
async def test_batch_any_unsafe_blocks_all(safety_gate):
    """Birden fazla görselde BİRİ bile unsafe ise tüm batch ENGELLENMELİ"""
    call_count = 0

    async def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        cats = MagicMock()
        # İkinci çağrıda silah tespit et
        cats.sexual = False
        cats.violence = False
        cats.hate = False
        cats.harassment = False
        cats.self_harm = False
        cats.illicit = (call_count == 2)  # Sadece 2. görselde

        mock_result = MagicMock()
        mock_result.categories = cats
        mock_result.category_scores = MagicMock(sexual=0.0, violence=0.0, hate=0.0)

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        return mock_response

    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = AsyncMock(side_effect=mock_create)

    result = await safety_gate.check_media([
        "https://example.com/safe_product.jpg",
        "https://example.com/gun.jpg",
    ])
    assert result["safe"] is False
    assert result["allow_listing"] is False


# ─────────────────────────────────────────────
# Moderation API input formatı
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_moderation_called_with_image_url_format(safety_gate):
    """Moderation API'si plain string değil, image_url objesi ile çağrılmalı"""
    cats = MagicMock()
    cats.sexual = False
    cats.violence = False
    cats.hate = False
    cats.harassment = False
    cats.self_harm = False
    cats.illicit = False

    mock_result = MagicMock()
    mock_result.categories = cats
    mock_result.category_scores = MagicMock(sexual=0.0, violence=0.0, hate=0.0)
    mock_response = MagicMock()
    mock_response.results = [mock_result]

    create_mock = AsyncMock(return_value=mock_response)
    safety_gate.client.moderations = MagicMock()
    safety_gate.client.moderations.create = create_mock

    await safety_gate._check_single_image("https://example.com/product.jpg")

    # API'nin image_url formatıyla çağrıldığını doğrula (plain string değil)
    call_kwargs = create_mock.call_args
    input_arg = call_kwargs.kwargs.get("input") or (call_kwargs.args[0] if call_kwargs.args else None)
    assert isinstance(input_arg, list), "input plain string değil liste olmalı"
    assert input_arg[0].get("type") == "image_url", "type='image_url' olmalı"
    assert "image_url" in input_arg[0], "image_url objesi içinde olmalı"


# ─────────────────────────────────────────────
# Türkçe mesajlar
# ─────────────────────────────────────────────

def test_block_messages_are_turkish(safety_gate):
    """Engelleme mesajları Türkçe olmalı"""
    for category_set in [{"sexual"}, {"violence"}, {"hate"}, {"illicit"}, {"harassment"}]:
        msg = safety_gate._get_block_message(category_set)
        assert len(msg) > 0
        # Türkçe karakterler veya Türkçe kelimeler içermeli
        assert any(word in msg.lower() for word in ["içerik", "şiddet", "paylaşılamaz", "yasadışı", "nefret", "güvenlik"])


# ─────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────

def test_singleton_instance():
    """vision_safety_gate singleton mevcut olmalı"""
    from agents.vision_safety_gate import vision_safety_gate
    assert vision_safety_gate is not None
    assert isinstance(vision_safety_gate, VisionSafetyGate)
