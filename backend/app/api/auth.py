from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.security import clear_session, issue_session, require_admin, verify_admin_key
from app.schemas import LoginRequest, MessageResponse

router = APIRouter()


@router.post("/login", response_model=MessageResponse)
async def login(payload: LoginRequest, response: Response) -> MessageResponse:
    if not verify_admin_key(payload.admin_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key.")
    issue_session(response)
    return MessageResponse(message="Logged in.")


@router.post("/logout", response_model=MessageResponse)
async def logout(response: Response) -> MessageResponse:
    clear_session(response)
    return MessageResponse(message="Logged out.")


@router.get("/me", response_model=MessageResponse)
async def me(_: dict = Depends(require_admin)) -> MessageResponse:
    return MessageResponse(message="Authenticated.")
