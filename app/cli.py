import argparse
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Business, User
from app.pipeline import router


def ensure_demo(s, user_ref: str):
    user = s.scalar(select(User).where(User.channel == "mock", User.channel_user_id == user_ref))
    if user is None:
        user = User(channel="mock", channel_user_id=user_ref, language="en")
        s.add(user)
        s.flush()
        s.add(Business(owner_user_id=user.id, name="Demo Shop"))
        s.commit()
    biz = s.scalar(select(Business).where(Business.owner_user_id == user.id))
    return user, biz


def simulate(text: str, user_ref: str, lang: str) -> None:
    with SessionLocal() as s:
        user, biz = ensure_demo(s, user_ref)
        queue = router.handle_text(s, biz.id, user.id, text, f"cli-{uuid.uuid4().hex}", lang)
        while queue:
            r = queue.pop(0)
            print("\n" + r.text)
            if r.kind == "confirm":
                print("[Save] [Edit] [Cancel]")
                choice = input("save / edit / cancel > ").strip().lower()
                for o in router.handle_button(s, biz.id, user.id, choice, r.pending_id, lang):
                    print("\n" + o.text)
            elif r.kind == "clarify":
                answer = input("your answer > ").strip()
                if answer:
                    queue = router.handle_text(s, biz.id, user.id, answer,
                                               f"cli-{uuid.uuid4().hex}", lang) + queue


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sim = sub.add_parser("simulate", help="run a transcript through the whole flow")
    sim.add_argument("text")
    sim.add_argument("--user", default="demo")
    sim.add_argument("--lang", default="en")
    args = parser.parse_args()
    if args.cmd == "simulate":
        simulate(args.text, args.user, args.lang)


if __name__ == "__main__":
    main()