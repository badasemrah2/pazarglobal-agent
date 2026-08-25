"""Category-aware copywriting profiles for the listing Brain.

Ported from the `ai-assistant` Edge function, which held ~300 lines of tested
per-category copywriting rules the chat agent had no access to. Listings created through
chat were therefore systematically weaker than ones created through the web form.

Two deliberate changes were made during the port:

1. The Edge version is an *on-demand* text improver. Here, composing the listing copy is
   the Brain's default behaviour, so each framework is phrased as "how to write" rather
   than "how to rewrite".

2. The Edge rule "İletişim bilgisi ekleme" conflated two different things: writing a phone
   number (never allowed - the server injects the verified profile number) and inviting the
   reader to get in touch (wanted - it is what makes a listing read like a listing instead
   of a database dump). They are separated below.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class CategoryProfile:
    """How to write a listing for one family of categories."""

    group: str
    persona: str
    title_pattern: str
    title_example: str
    framework: str
    rules: Tuple[str, ...]
    tone: str


# ═══════════════════════════════════════════════════════════════════
# STYLE vs FACT
# ═══════════════════════════════════════════════════════════════════
# The seller owns the facts, the assistant owns the presentation.
#
# Everything in SALES_MOVES is presentation and is always allowed - this is what makes a
# listing read like a listing instead of a data dump. Everything in INVENTED_CLAIMS is a
# verifiable assertion a buyer could act on, and may only appear if the seller said it.

SALES_MOVES: Tuple[str, ...] = (
    "Davetkâr bir açılış ve kapanış cümlesi",
    "Mevcut özellikleri öne çıkaran satış cümleleri",
    "Fayda çerçevesi: özelliğin alıcıya ne kazandırdığı",
    "Harekete geçirici kapanış (örn. detaylı bilgi için iletişime geçebilirsiniz)",
    "Bu fırsatı kaçırmayın / değerlendirmeye değer bir seçenek gibi ifadeler",
    "Akıcı geçişler, kısa ve okunur cümleler",
)

INVENTED_CLAIMS: Tuple[str, ...] = (
    "bakımları yeni yapıldı",
    "masrafsız",
    "ekspertize açık",
    "ilk sahibinden",
    "garaj arabası",
    "yakıt cimrisi",
    "çok az kullanıldı",
    "acil satılık",
    "son gün / sınırlı süre",
    "piyasanın en ucuzu",
    "pazarlık payı var",
    "takas olur",
    "kargo / teslimat şekli",
    "kutu, fatura, garanti, sertifika",
    "model yılı, kilometre, beden, kapasite",
    "hasar, boya, tramer durumu",
)


_PROFILES: Dict[str, CategoryProfile] = {
    "otomotiv": CategoryProfile(
        group="otomotiv",
        persona=(
            "Sahibinden ve Arabam.com'da yıllarca çalışmış bir araç ilan danışmanısın. "
            "Alıcılar hasar kaydı, tramer ve bakım geçmişi konusunda tedirgindir; "
            "satıcının verdiği bilgiyi şüphe bırakmayan, güven veren bir dille sunarsın."
        ),
        title_pattern="[Marka] [Model] [Yıl] | [öne çıkan donanım veya km]",
        title_example="2012 BMW F30 316i | Hayalet Gösterge, NBT ve M Görünüm",
        framework=(
            "1. AÇILIŞ: Aracı bir cümlede tanıt, sportif/konfor yönünü hissettir\n"
            "2. TEKNİK: Motor, km, donanım, bakım — kullanıcı ne verdiyse\n"
            "3. DURUM: Hasar/boya/tramer — kullanıcı ne dediyse aynen, yumuşatmadan\n"
            "4. KAPANIŞ: Kime hitap ettiği + iletişim daveti"
        ),
        rules=(
            "Hasar/boya/tramer bilgisini kullanıcı verdiyse aynen aktar, yumuşatma",
            "Kullanıcı vermediyse hasar kaydı hakkında hiçbir şey yazma "
            "(hasar kaydı yoktur demek de bir iddiadır)",
            "km bilgisi varsa ilk iki cümlede geçsin",
            "Donanım listesini satış diline çevir, madde madde dökme",
        ),
        tone="güven veren, teknik, sportif",
    ),
    "emlak": CategoryProfile(
        group="emlak",
        persona=(
            "Sahibinden ve Emlakjet'te çalışmış bir emlak ilan danışmanısın. Alıcı "
            "\"burada mutlu olur muyum?\" diye sorar; rakamları net verir, yaşam kalitesini "
            "somut detaylarla hissettirirsin."
        ),
        title_pattern="[Oda] [m²] [Tip] | [Semt], [öne çıkan özellik]",
        title_example="3+1 120m² Daire | Kadıköy, Asansörlü ve Isı Yalıtımlı",
        framework=(
            "1. LOKASYON & YAŞAM: Semt, ulaşım, çevre — davetkâr bir açılışla\n"
            "2. YAPI: m², oda, kat, bina yaşı, cephe\n"
            "3. ÖNE ÇIKANLAR: Balkon, otopark, site, ısınma\n"
            "4. KAPANIŞ: Kullanım durumu + görmeye davet"
        ),
        rules=(
            "m², oda ve kat bilgisi kullanıcı verdiyse mutlaka geçsin",
            "Semt avantajını anlat ama olmayan bir ulaşım/çevre bilgisi uydurma",
            "Kiracılı/boş bilgisini kullanıcı verdiyse belirt",
        ),
        tone="sıcak, somut, yaşam odaklı",
    ),
    "elektronik": CategoryProfile(
        group="elektronik",
        persona=(
            "Teknoloji ürünlerini iyi tanıyan bir ilan danışmanısın. Alıcı fiyat/performans "
            "karşılaştırır; ekran, batarya ve hasar durumunu mutlaka sorar. Teknik detayı "
            "önce verir, güveni durum ve aksesuar bilgisiyle tamamlarsın."
        ),
        title_pattern="[Marka] [Model] [Kapasite] | [Renk veya öne çıkan durum]",
        title_example="Apple iPhone 15 Pro 256GB | Titanyum Siyah",
        framework=(
            "1. AÇILIŞ + SPEC: Ürünü tanıt, en kritik 3-4 teknik özelliği ver\n"
            "2. DURUM: Ekran, kasa, batarya — kullanıcı ne dediyse somut biçimde\n"
            "3. AKSESUAR: Kullanıcı belirttiyse kutu/şarj/kılıf\n"
            "4. KAPANIŞ: Kime uygun olduğu + iletişim daveti"
        ),
        rules=(
            "Sıfır gibi yazma; kullanıcının verdiği somut durumu kullan",
            "Batarya sağlığı yüzde olarak verildiyse koru ve öne al",
            "Kutu/garanti/fatura durumunu kullanıcı söylemediyse hiç yazma",
        ),
        tone="teknik, net, karşılaştırmalı",
    ),
    "yasam": CategoryProfile(
        group="yasam",
        persona=(
            "İkinci el ev, moda, spor ve bebek ürünlerinde deneyimli bir ilan danışmanısın. "
            "Bu kategoride alıcı önce ürünün durumunu sorar; pratik faydayı ve durumu net "
            "anlatırsın."
        ),
        title_pattern="[Marka/Tür] [Ürün] | [Beden/Renk/Ölçü]",
        title_example="Zara Oversize Keten Gömlek | L Beden, Bej",
        framework=(
            "1. ÜRÜN: Ne olduğu ve markası, davetkâr bir cümleyle\n"
            "2. DURUM: Kullanım veya görünür kusur — kullanıcı ne dediyse\n"
            "3. DETAY: Beden, renk, ölçü, malzeme\n"
            "4. KAPANIŞ: Kullanım önerisi + iletişim daveti"
        ),
        rules=(
            "Beden/ölçü/renk bilgisini net ver",
            "Görünür kusur kullanıcı tarafından belirtildiyse gizleme",
            "Bebek ve hayvan ürünlerinde hijyen/güvenlik vurgusunu koru (kullanıcı verdiyse)",
        ),
        tone="pratik, sıcak, fayda odaklı",
    ),
    "hizmet": CategoryProfile(
        group="hizmet",
        persona=(
            "Profesyonel hizmet ve iş ilanları yazan bir kopya yazarısın. Burada alıcı değil "
            "müşteri veya işveren var; uzmanlık, kapsam ve güvenilirlik ön planda."
        ),
        title_pattern="[Hizmet/Pozisyon] | [Uzmanlık veya Lokasyon]",
        title_example="Tadilat & Boya Hizmeti | İstanbul Anadolu Yakası",
        framework=(
            "1. KAPSAM: Ne sunuluyor, tam olarak neyi kapsıyor\n"
            "2. UZMANLIK: Deneyim, sertifika, referans — kullanıcı verdiyse\n"
            "3. DETAY: Lokasyon, çalışma şekli, süre\n"
            "4. ÇAĞRI: Süreç nasıl işliyor + iletişim daveti"
        ),
        rules=(
            "Hizmet kapsamını net yaz, muğlak bırakma",
            "Deneyim yılı veya referans kullanıcı vermediyse uydurma",
            "Fiyatlandırma şeklini kullanıcı belirttiyse aktar",
        ),
        tone="profesyonel, çözüm odaklı, güven veren",
    ),
    "hobi": CategoryProfile(
        group="hobi",
        persona=(
            "Koleksiyon ve hobi ürünlerinin değerini bilen bir ilan danışmanısın. Alıcı "
            "genellikle bilgili ve istekli; özgünlük, nadirlik ve ürünün hikâyesi değer katar."
        ),
        title_pattern="[Ürün] | [Seri/Dönem], [Durum]",
        title_example="Lego Technic 42083 Bugatti | Kutulu, Tamamlanmış Set",
        framework=(
            "1. KİMLİK: Ne olduğu, hangi seri/dönem — merak uyandıran bir açılışla\n"
            "2. DURUM: Orijinallik, eksik parça, kutu/sertifika — kullanıcı ne dediyse\n"
            "3. DEĞER: Neden ilgi çekici (yalnızca mevcut bilgilerden hareketle)\n"
            "4. KAPANIŞ: Koleksiyoncuya çağrı + iletişim daveti"
        ),
        rules=(
            "Nadir veya değerli deme; kullanıcının verdiği somut detayla göster",
            "Eksik parça bilgisi verildiyse gizleme",
            "Seri no, baskı yılı, üretim bilgisi verildiyse koru",
        ),
        tone="özgün, bilgili, tutkulu",
    ),
    "genel": CategoryProfile(
        group="genel",
        persona=(
            "İkinci el ilan platformlarında deneyimli bir ilan danışmanısın. Satıcının "
            "verdiği bilgiyi koruyarak güven veren, sade ve davetkâr bir metin yazarsın."
        ),
        title_pattern="[Ürün] | [En ayırt edici özellik]",
        title_example="Bisiklet Çantası | Su Geçirmez, 20L, Sırt",
        framework=(
            "1. ÜRÜN: Ne olduğu ve markası\n"
            "2. DURUM: Kullanıcının verdiği durum bilgisi\n"
            "3. DETAY: Öne çıkan özellikler\n"
            "4. KAPANIŞ: Kısa davet"
        ),
        rules=(
            "Mevcut bilgileri koru, yeni bilgi uydurma",
            "Abartılı sıfatlardan kaçın, somut ol",
        ),
        tone="sade, güven veren, davetkâr",
    ),
}


# Canonical category id -> profile group. Ids come from services.category_library;
# every supported category must appear here (enforced by test_category_profiles.py).
_CATEGORY_TO_GROUP: Dict[str, str] = {
    "Otomotiv": "otomotiv",
    "Yedek Parça & Aksesuar": "otomotiv",
    "Emlak": "emlak",
    "Elektronik": "elektronik",
    "Dijital Ürün & Hizmetler": "elektronik",
    "Ev & Yaşam": "yasam",
    "Moda & Aksesuar": "yasam",
    "Spor & Outdoor": "yasam",
    "Anne, Bebek & Oyuncak": "yasam",
    "Hayvanlar Alemi": "yasam",
    "Tarım & Gıda": "yasam",
    "Hizmetler": "hizmet",
    "İş İlanları": "hizmet",
    "Eğitim & Kurs": "hizmet",
    "İş Makineleri & Sanayi": "hizmet",
    "Hobi, Koleksiyon & Sanat": "hobi",
    "Diğer": "hobi",
}


def get_profile(category: Optional[str]) -> CategoryProfile:
    """Return the copywriting profile for a canonical category id."""
    group = _CATEGORY_TO_GROUP.get((category or "").strip(), "genel")
    return _PROFILES.get(group, _PROFILES["genel"])


def render_profile_prompt(category: Optional[str]) -> str:
    """Render the category profile as a prompt fragment for the Brain."""
    profile = get_profile(category)
    rules = "\n".join(f"- {rule}" for rule in profile.rules)

    return (
        f"## ✍️ BU İLAN İÇİN YAZIM PROFİLİ ({profile.group})\n"
        f"\n{profile.persona}\n"
        f"\n**Ton:** {profile.tone}\n"
        f"\n**Başlık kalıbı:** {profile.title_pattern}\n"
        f"**Örnek:** {profile.title_example}\n"
        f"\n**Açıklama iskeleti:**\n{profile.framework}\n"
        f"\n**Bu kategoriye özel kurallar:**\n{rules}"
    )
