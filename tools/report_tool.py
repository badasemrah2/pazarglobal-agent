"""
Illegal listing report tool — illegal_reports tablosuna kullanıcı şikayeti kaydeder.

Kullanıcı WhatsApp veya WebChat üzerinden bir ilanı şikayet etmek istediğinde
bu tool çalışır. Şikayet illegal_reports tablosuna "reviewed=false" olarak düşer,
admin panelinden incelenmeyi bekler.
"""
from typing import Dict, Any, Optional
from loguru import logger
from .base_tool import BaseTool
from services import supabase_client


class ReportIllegalListingTool(BaseTool):
    """Kullanıcının bir ilanı yasa dışı/uygunsuz olarak şikayet etmesini sağlar."""

    def get_name(self) -> str:
        return "report_illegal_listing"

    def get_description(self) -> str:
        return (
            "Bir ilanı yasa dışı, sahte veya uygunsuz içerik içerdiği gerekçesiyle şikayet eder. "
            "Kullanıcı 'şikayet et', 'ihbar et', 'yasadışı', 'dolandırıcık' gibi ifadeler kullandığında "
            "bu tool çağrılmalıdır. Şikayet admin ekibine iletilir ve incelenir."
        )

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reporter_user_id": {
                    "type": "string",
                    "description": "Şikayeti yapan kullanıcının user_id veya phone_number değeri"
                },
                "listing_id": {
                    "type": "string",
                    "description": "Şikayet edilen ilanın UUID'si"
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Şikayet sebebi. Örnek: 'silah satışı', 'sahte ilan', "
                        "'uyuşturucu', 'dolandırıcılık', 'hakaret', 'çalıntı ürün'"
                    )
                },
                "evidence_description": {
                    "type": "string",
                    "description": "Varsa ek açıklama veya kanıt bilgisi (opsiyonel)"
                }
            },
            "required": ["reporter_user_id", "listing_id", "reason"]
        }

    async def execute(
        self,
        reporter_user_id: str,
        listing_id: str,
        reason: str,
        evidence_description: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            evidence: Optional[Dict[str, Any]] = None
            if evidence_description:
                evidence = {"description": evidence_description}

            report = await supabase_client.create_illegal_report(
                reporter_user_id=reporter_user_id,
                listing_id=listing_id,
                reason=reason,
                evidence=evidence,
            )

            if report:
                logger.info(
                    f"📋 illegal_reports kaydedildi: id={report.get('id')} "
                    f"listing={listing_id} reporter={reporter_user_id}"
                )
                return self.format_success({
                    "report_id": report.get("id"),
                    "message": (
                        "✅ Şikayetiniz alındı ve incelemeye alınacak. "
                        "Ekibimiz en kısa sürede değerlendirecek."
                    )
                })
            else:
                return self.format_error("Şikayet kaydedilemedi. Lütfen tekrar deneyin.")

        except Exception as e:
            logger.error(f"ReportIllegalListingTool execute hatası: {e}")
            return self.format_error(f"Şikayet sırasında hata oluştu: {str(e)}")


class GetIllegalReportsTool(BaseTool):
    """Admin: Belirli bir ilana ait şikayetleri listeler (dahili kullanım)."""

    def get_name(self) -> str:
        return "get_illegal_reports"

    def get_description(self) -> str:
        return (
            "Admin aracı: Bir ilana veya tüm şikayetleri getirir. "
            "Sadece admin/moderatör akışlarında kullanılmalıdır."
        )

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "listing_id": {
                    "type": "string",
                    "description": "Belirli bir ilana ait şikayetler (opsiyonel)"
                },
                "reviewed": {
                    "type": "boolean",
                    "description": "true=incelenmiş, false=bekleyen (opsiyonel, varsayılan: tümü)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maksimum sonuç sayısı (varsayılan: 20)",
                    "default": 20
                }
            },
            "required": []
        }

    async def execute(
        self,
        listing_id: Optional[str] = None,
        reviewed: Optional[bool] = None,
        limit: int = 20,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            reports = await supabase_client.get_illegal_reports(
                listing_id=listing_id,
                reviewed=reviewed,
                limit=limit,
            )
            return self.format_success({
                "reports": reports,
                "count": len(reports),
            })
        except Exception as e:
            logger.error(f"GetIllegalReportsTool execute hatası: {e}")
            return self.format_error(str(e))


# Singleton instances
report_illegal_listing_tool = ReportIllegalListingTool()
get_illegal_reports_tool = GetIllegalReportsTool()
