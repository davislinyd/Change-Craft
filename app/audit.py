from app.models import Event


def log(db, actor, action: str, *, change=None, entity=None, source: str = "web", **detail):
    db.add(
        Event(
            change_id=change.id if change else None,
            entity_type=type(entity).__name__ if entity else None,
            entity_id=entity.id if entity else None,
            action=action,
            actor_id=actor.id if actor else None,
            source=source,
            detail={k: v if isinstance(v, (bool, int, type(None))) else str(v) for k, v in detail.items()},
        )
    )
