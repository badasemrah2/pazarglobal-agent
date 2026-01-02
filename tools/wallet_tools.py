"""
Wallet and credit management tools
"""
from typing import Dict, Any
from loguru import logger
from .base_tool import BaseTool
from services import supabase_client
from services.supabase_client import InsufficientCreditsError


class GetWalletBalanceTool(BaseTool):
    """Tool to get user's wallet balance"""
    
    def get_name(self) -> str:
        return "get_wallet_balance"
    
    def get_description(self) -> str:
        return "Get the current wallet balance for a user"
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID"
                }
            },
            "required": ["user_id"]
        }
    
    async def execute(self, user_id: str) -> Dict[str, Any]:
        balance = await supabase_client.get_wallet_balance(user_id)
        if balance is not None:
            return self.format_success({"balance": balance})
        return self.format_error("Failed to get wallet balance")


class DeductCreditsTool(BaseTool):
    """Tool to deduct credits from user wallet"""
    
    def get_name(self) -> str:
        return "deduct_credits"
    
    def get_description(self) -> str:
        return "Deduct credits from user's wallet for listing publication or other actions"
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID"
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount of credits to deduct"
                },
                "description": {
                    "type": "string",
                    "description": "Description of the transaction"
                }
            },
            "required": ["user_id", "amount", "description"]
        }
    
    async def execute(self, user_id: str, amount: int, description: str) -> Dict[str, Any]:
        try:
            await supabase_client.deduct_credits(user_id, amount, description)
        except InsufficientCreditsError as exc:
            return self.format_error(str(exc))
        except Exception as exc:
            logger.error(f"Deduct credits tool failed: {exc}")
            return self.format_error("Kredi düşülürken beklenmeyen bir hata oluştu.")

        return self.format_success({
            "deducted": amount,
            "description": description,
            "message": "Credits deducted successfully"
        })


# Tool instances
get_wallet_balance_tool = GetWalletBalanceTool()
deduct_credits_tool = DeductCreditsTool()
