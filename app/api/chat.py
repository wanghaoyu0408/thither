"""The conversation endpoint (spec section 32).

Loads TripState, runs one agent turn, applies whatever patches the agent
proposes through the normal engine, and returns the reply plus what actually
happened to the trip.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runner import AgentRunner
from app.config import get_settings
from app.db.models import TripMessageRow
from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session, get_sessionmaker
from app.models.chat import ChatMessage, ChatRequest, ChatResponse, ToolCallSummary
from app.providers.openai_llm import OpenAIClient
from app.services.toolbox import MissingCredentials, Toolbox

router = APIRouter(prefix="/trips", tags=["chat"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load_history(session: AsyncSession, trip_id: str, turns: int) -> list[dict[str, Any]]:
    if turns <= 0:
        return []
    rows = await session.execute(
        select(TripMessageRow)
        .where(TripMessageRow.trip_id == trip_id)
        .order_by(TripMessageRow.id.desc())
        .limit(turns * 2)
    )
    messages = list(rows.scalars())[::-1]
    return [{"role": row.role, "content": row.content} for row in messages]


@router.post("/{trip_id}/messages", response_model=ChatResponse)
async def send_message(trip_id: str, payload: ChatRequest, session: SessionDep) -> ChatResponse:
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENAI_API_KEY is not set; add it to .env to use the conversation endpoint.",
        )

    repo = TripRepository(session)
    try:
        state = await repo.get(trip_id)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None

    try:
        toolbox = Toolbox(settings, get_sessionmaker())
    except MissingCredentials as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None

    history = await _load_history(session, trip_id, payload.history_turns)

    async with toolbox:
        runner = AgentRunner(
            OpenAIClient(settings.openai_api_key, settings.openai_model),
            toolbox,
            session,
            settings=settings,
        )
        run = await runner.run(state, payload.message, history=history)

    user_message = ChatMessage(role="user", content=payload.message)
    reply_message = ChatMessage(role="assistant", content=run.reply)

    session.add(
        TripMessageRow(
            trip_id=trip_id,
            message_id=user_message.message_id,
            role="user",
            content=payload.message,
            revision=run.revision_before,
        )
    )
    session.add(
        TripMessageRow(
            trip_id=trip_id,
            message_id=reply_message.message_id,
            role="assistant",
            content=run.reply,
            revision=run.revision_after,
            run_log=run.as_log(),
        )
    )
    await session.commit()

    return ChatResponse(
        reply=run.reply,
        trip_id=trip_id,
        revision_before=run.revision_before,
        revision_after=run.revision_after,
        changed_state=run.changed_state,
        tools_called=[
            ToolCallSummary(name=t.name, milliseconds=t.milliseconds, ok=t.ok, detail=t.detail)
            for t in run.tools
        ],
        patches_applied=sum(1 for patch in run.patches if patch.applied),
        patches_rejected=[
            [error.code for error in patch.errors] for patch in run.patches if not patch.applied
        ],
        iterations=run.iterations,
        hit_iteration_limit=run.hit_iteration_limit,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        error=run.error,
    )


@router.get("/{trip_id}/messages", response_model=list[ChatMessage])
async def list_messages(
    trip_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ChatMessage]:
    rows = await session.execute(
        select(TripMessageRow)
        .where(TripMessageRow.trip_id == trip_id)
        .order_by(TripMessageRow.id)
        .limit(limit)
    )
    return [
        ChatMessage(
            message_id=row.message_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
        )
        for row in rows.scalars()
    ]
