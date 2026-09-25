"""Wine tasting app: FastAPI on Render, Postgres on Supabase.

Guests never see wine identities before the reveal: the browser only receives
pour numbers, and the database is closed to the anon key except event_state.
"""
from __future__ import annotations

import csv
import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional


def load_local_env(env_file: Path | str | None = None):
    """Load environment variables from a project-local env file for local development."""
    project_root = Path(__file__).resolve().parent.parent
    target = Path(env_file) if env_file else project_root / ".env.local"

    if not target.exists():
        return

    for line in target.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        os.environ.setdefault(key, value)


load_local_env()

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import analytics

STATIC = Path(__file__).parent / "static"
pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(_app):
    global pool
    # prepare_threshold=None keeps the Supabase transaction pooler (port 6543) happy.
    # timeout=5: a bad DATABASE_URL fails /healthz fast instead of hanging 30s.
    pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=10, timeout=5,
                          kwargs={"row_factory": dict_row, "prepare_threshold": None})
    yield
    pool.close()


app = FastAPI(title="Wine tasting", lifespan=lifespan)


def q(sql: str, params=(), one=False):
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


def require_host(x_host_pin: str = Header(default="")):
    if not secrets.compare_digest(x_host_pin, os.environ.get("HOST_PIN", "")):
        raise HTTPException(401, "Wrong host PIN")


def get_event(event_id: str) -> dict:
    ev = q("select * from events where id = %s", (event_id,), one=True)
    if not ev:
        raise HTTPException(404, "No tasting with that ID")
    return ev


def round_count(event_id: str) -> int:
    return q("select coalesce(max(round_no), 1) as n from wines where event_id = %s",
             (event_id,), one=True)["n"]


def refresh_state(event_id: str, reveal_step: int | None = None):
    """Recompute the public progress row that phones watch through Realtime."""
    q("""
      with e as (select * from events where id = %(id)s),
           rw as (select w.id from wines w, e where w.event_id = e.id and w.round_no = e.current_round),
           rs as (select s.member_id from scores s where s.wine_id in (select id from rw))
      insert into event_state (event_id, status, current_round, round_count, reveal_step,
                               wine_count, tasters, finished, updated_at)
      select e.id, e.status, e.current_round,
             (select coalesce(max(round_no), 1) from wines where event_id = e.id),
             coalesce(%(step)s, (select reveal_step from event_state where event_id = e.id), 0),
             (select count(*) from rw),
             (select count(distinct member_id) from rs),
             (select count(*) from (select member_id from rs group by member_id
                                    having count(*) >= (select count(*) from rw)) f),
             now()
      from e
      on conflict (event_id) do update set
        status = excluded.status, current_round = excluded.current_round,
        round_count = excluded.round_count, reveal_step = excluded.reveal_step,
        wine_count = excluded.wine_count, tasters = excluded.tasters,
        finished = excluded.finished, updated_at = now()
    """, {"id": event_id, "step": reveal_step})
    return q("select * from event_state where event_id = %s", (event_id,), one=True)


def frames(event_id: str):
    scores = pd.DataFrame(q("""
        select s.*, m.name as member_name from scores s join members m on m.id = s.member_id
        where s.event_id = %s""", (event_id,)))
    wines = pd.DataFrame(q("select * from wines where event_id = %s order by pour_no", (event_id,)))
    if scores.empty:
        scores = pd.DataFrame(columns=["member_id", "wine_id", "total", "price_guess", *analytics.CATEGORIES])
    for df, cols in ((scores, ["total", "price_guess", *analytics.CATEGORIES]), (wines, ["price", "abv"])):
        for c in cols:
            if c in df:
                df[c] = pd.to_numeric(df[c])
    for df, cols in ((scores, ["member_id", "wine_id"]), (wines, ["id", "twin_of"])):
        for c in cols:
            if c in df:
                df[c] = df[c].map(lambda v: None if v is None else str(v))
    names = {str(m["id"]): m["name"] for m in q("select id, name from members")}
    return scores, wines, names


def all_rounds(event_id: str, member_id: str | None = None):
    """Every round's reveal steps, plus how many of each are showing right now."""
    ev = get_event(event_id)
    state = q("select reveal_step from event_state where event_id = %s", (event_id,), one=True)
    scores, wines, names = frames(event_id)
    n_rounds = int(wines["round_no"].max()) if not wines.empty else 1
    mine = analytics.member_view(scores, member_id) if member_id else {}
    rounds = []
    for r in range(1, n_rounds + 1):
        steps = analytics.reveal_steps(scores, wines, names, r, final=(r == n_rounds)) if not wines.empty else []
        for st in steps:
            if st["kind"] == "wine":
                st["yours"] = mine.get(st["wine_id"])
        if r < ev["current_round"] or ev["status"] == "done":
            shown = len(steps)
        elif r == ev["current_round"] and ev["status"] == "revealing":
            shown = min(state["reveal_step"] if state else 0, len(steps))
        else:
            shown = 0
        rounds.append({"round": r, "steps": steps, "shown": shown, "final": r == n_rounds})
    return ev, rounds


# ---------- pages ----------

# no-cache: browsers revalidate, so a deploy reaches phones on the next load.
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def guest_page():
    return FileResponse(STATIC / "guest.html", headers=NO_CACHE)


@app.get("/host")
def host_page():
    return FileResponse(STATIC / "host.html", headers=NO_CACHE)


@app.get("/healthz")
def health():
    q("select 1")
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"supabase_url": os.environ.get("SUPABASE_URL"),
            "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY")}


# ---------- guests ----------

@app.get("/api/join/{code}")
def join(code: str):
    ev = q("""select id, name, theme, held_on, location, status, current_round
              from events where lower(join_code) = lower(%s)""", (code.strip(),), one=True)
    if not ev:
        raise HTTPException(404, "That code doesn't match a tasting. Check it with the host.")
    wines = q("select id, pour_no, round_no from wines where event_id = %s order by pour_no", (ev["id"],))
    members = q("select id, name from members order by name")
    state = q("select * from event_state where event_id = %s", (ev["id"],), one=True)
    return {"event": ev, "wines": wines, "members": members, "state": state,
            "max": analytics.CATEGORIES}


class NewMember(BaseModel):
    name: str = Field(min_length=1, max_length=40)


@app.post("/api/members")
def add_member(body: NewMember):
    name = " ".join(body.name.split())
    existing = q("select id, name from members where lower(name) = lower(%s)", (name,), one=True)
    return existing or q("insert into members (name) values (%s) returning id, name", (name,), one=True)


class ScoreIn(BaseModel):
    event_id: str
    wine_id: str
    member_id: str
    appearance: Optional[float] = Field(default=None, ge=0, le=3)
    aroma: Optional[float] = Field(default=None, ge=0, le=6)
    taste: Optional[float] = Field(default=None, ge=0, le=6)
    aftertaste: Optional[float] = Field(default=None, ge=0, le=3)
    overall: Optional[float] = Field(default=None, ge=0, le=2)
    total: Optional[float] = Field(default=None, ge=0, le=20)   # paper sheets with only a total
    price_guess: Optional[float] = Field(default=None, ge=0, le=10000)
    note: Optional[str] = Field(default=None, max_length=500)


def save_score(s: ScoreIn, entered_by: str, allow_total_only: bool):
    cats = {c: getattr(s, c) for c in analytics.CATEGORIES}
    given = [v for v in cats.values() if v is not None]
    if given and len(given) < len(cats):
        missing = [c for c, v in cats.items() if v is None]
        raise HTTPException(422, "Score every category, or none: missing " + ", ".join(missing))
    if given:
        if any((v * 2) % 1 for v in given):
            raise HTTPException(422, "Scores go in half-point steps")
        total = sum(given)
    elif allow_total_only and s.total is not None:
        if (s.total * 2) % 1:
            raise HTTPException(422, "Scores go in half-point steps")
        total = s.total
    else:
        raise HTTPException(422, "Score every category to save")

    ev = get_event(s.event_id)
    wine = q("select id, round_no from wines where id = %s and event_id = %s",
             (s.wine_id, s.event_id), one=True)
    if not wine:
        raise HTTPException(404, "That wine isn't part of this tasting")
    if wine["round_no"] != ev["current_round"]:
        raise HTTPException(409, f"Wine is in round {wine['round_no']}; round {ev['current_round']} is being tasted")
    allowed = ("open",) if entered_by == "self" else ("open", "locked")
    if ev["status"] not in allowed:
        msg = {"setup": "Scoring hasn't opened yet",
               "locked": "Scoring is closed for this round",
               "revealing": "Results are frozen once the reveal starts",
               "done": "This tasting is finished"}[ev["status"]]
        raise HTTPException(409, msg)
    q("""
      insert into scores (event_id, wine_id, member_id, appearance, aroma, taste, aftertaste,
                          overall, total, price_guess, note, entered_by)
      values (%(event_id)s, %(wine_id)s, %(member_id)s, %(appearance)s, %(aroma)s, %(taste)s,
              %(aftertaste)s, %(overall)s, %(t)s, %(price_guess)s, %(note)s, %(by)s)
      on conflict (wine_id, member_id) do update set
        appearance = excluded.appearance, aroma = excluded.aroma, taste = excluded.taste,
        aftertaste = excluded.aftertaste, overall = excluded.overall, total = excluded.total,
        price_guess = excluded.price_guess, note = excluded.note,
        entered_by = excluded.entered_by, updated_at = now()
    """, {**s.model_dump(), "t": total, "by": entered_by})
    refresh_state(s.event_id)
    return {"total": total}


@app.put("/api/scores")
def put_score(s: ScoreIn):
    return save_score(s, "self", allow_total_only=False)


@app.get("/api/events/{event_id}/members/{member_id}/scores")
def my_scores(event_id: str, member_id: str):
    return q("""select wine_id, appearance, aroma, taste, aftertaste, overall, total, price_guess, note
                from scores where event_id = %s and member_id = %s""", (event_id, member_id))


@app.get("/api/events/{event_id}/reveal")
def reveal(event_id: str, member_id: Optional[str] = None):
    ev, rounds = all_rounds(event_id, member_id)
    return {"status": ev["status"], "current_round": ev["current_round"],
            "rounds": [{"round": r["round"], "final": r["final"], "total_steps": len(r["steps"]),
                        "steps": r["steps"][:r["shown"]]} for r in rounds if r["shown"]]}


# ---------- host: events ----------

@app.get("/api/host/check", dependencies=[Depends(require_host)])
def host_check():
    return {"ok": True}


@app.get("/api/host/events", dependencies=[Depends(require_host)])
def list_events():
    return q("""select e.*, (select count(*) from wines w where w.event_id = e.id) as wine_count,
                       (select coalesce(max(round_no), 0) from wines w where w.event_id = e.id) as rounds
                from events e order by held_on desc, created_at desc""")


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    held_on: date
    theme: Optional[str] = None
    location: Optional[str] = None
    join_code: Optional[str] = Field(default=None, max_length=12)


@app.post("/api/host/events", dependencies=[Depends(require_host)])
def create_event(e: EventIn):
    code = (e.join_code or secrets.token_hex(2)).strip().upper()
    if q("select 1 from events where lower(join_code) = lower(%s)", (code,), one=True):
        raise HTTPException(409, f"Join code {code} is already used by another tasting")
    ev = q("""insert into events (name, held_on, theme, location, join_code)
              values (%s, %s, %s, %s, %s) returning *""",
           (e.name, e.held_on, e.theme, e.location, code), one=True)
    refresh_state(str(ev["id"]))
    return ev


@app.patch("/api/host/events/{event_id}", dependencies=[Depends(require_host)])
def update_event(event_id: str, e: EventIn):
    get_event(event_id)
    code = (e.join_code or "").strip().upper()
    if code and q("select 1 from events where lower(join_code) = lower(%s) and id <> %s",
                  (code, event_id), one=True):
        raise HTTPException(409, f"Join code {code} is already used by another tasting")
    return q("""update events set name = %s, held_on = %s, theme = %s, location = %s,
                join_code = coalesce(nullif(%s, ''), join_code) where id = %s returning *""",
             (e.name, e.held_on, e.theme, e.location, code, event_id), one=True)


@app.get("/api/host/events/{event_id}", dependencies=[Depends(require_host)])
def event_detail(event_id: str):
    ev = get_event(event_id)
    wines = q("select * from wines where event_id = %s order by round_no, pour_no", (event_id,))
    return {"event": ev, "wines": wines, "state": refresh_state(event_id)}


# ---------- host: the flight ----------

class WineIn(BaseModel):
    round_no: int = Field(default=1, ge=1, le=20)
    pour_no: Optional[int] = Field(default=None, ge=1, le=99)   # blank: next number
    name: str = Field(min_length=1, max_length=120)
    producer: Optional[str] = None
    appellation: Optional[str] = None
    vintage: Optional[int] = Field(default=None, ge=1900, le=2100)
    price: Optional[float] = Field(default=None, ge=0)
    abv: Optional[float] = Field(default=None, ge=0, le=25)
    soil: Optional[str] = None
    notes: Optional[str] = None
    twin_of_pour: Optional[int] = None   # the secret duplicate: which pour it copies


def round_is_editable(ev: dict, round_no: int) -> bool:
    """Wines can be added, moved or removed only in rounds nobody has started scoring."""
    return round_no > ev["current_round"] or (round_no == ev["current_round"] and ev["status"] == "setup")


def resolve_twin(event_id: str, w: WineIn, own_id: str | None = None):
    if w.twin_of_pour is None:
        return None
    t = q("select id from wines where event_id = %s and pour_no = %s", (event_id, w.twin_of_pour), one=True)
    if not t or str(t["id"]) == own_id:
        raise HTTPException(422, f"There's no other wine poured as number {w.twin_of_pour}")
    return t["id"]


@app.post("/api/host/events/{event_id}/wines", dependencies=[Depends(require_host)])
def add_wine(event_id: str, w: WineIn):
    ev = get_event(event_id)
    if not round_is_editable(ev, w.round_no):
        raise HTTPException(409, f"Round {w.round_no} has already started. Add the wine to a later round.")
    pour = w.pour_no or q("select coalesce(max(pour_no), 0) + 1 as n from wines where event_id = %s",
                          (event_id,), one=True)["n"]
    if q("select 1 from wines where event_id = %s and pour_no = %s", (event_id, pour), one=True):
        raise HTTPException(409, f"Pour number {pour} is already taken")
    row = q("""insert into wines (event_id, round_no, pour_no, name, producer, appellation, vintage,
                                  price, abv, soil, notes, twin_of)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            (event_id, w.round_no, pour, w.name, w.producer, w.appellation, w.vintage, w.price,
             w.abv, w.soil, w.notes, resolve_twin(event_id, w)), one=True)
    refresh_state(event_id)
    return row


@app.patch("/api/host/wines/{wine_id}", dependencies=[Depends(require_host)])
def edit_wine(wine_id: str, w: WineIn):
    cur = q("select * from wines where id = %s", (wine_id,), one=True)
    if not cur:
        raise HTTPException(404, "No such wine")
    event_id = str(cur["event_id"])
    ev = get_event(event_id)
    pour = w.pour_no or cur["pour_no"]
    twin = resolve_twin(event_id, w, wine_id)
    structural = (w.round_no != cur["round_no"] or pour != cur["pour_no"] or twin != cur["twin_of"])
    if structural and not (round_is_editable(ev, cur["round_no"]) and round_is_editable(ev, w.round_no)):
        raise HTTPException(409, "Round and pour number are fixed once a round starts. Details can still change.")
    if pour != cur["pour_no"] and q("select 1 from wines where event_id = %s and pour_no = %s",
                                    (event_id, pour), one=True):
        raise HTTPException(409, f"Pour number {pour} is already taken")
    row = q("""update wines set round_no = %s, pour_no = %s, name = %s, producer = %s, appellation = %s,
                  vintage = %s, price = %s, abv = %s, soil = %s, notes = %s, twin_of = %s
               where id = %s returning *""",
            (w.round_no, pour, w.name, w.producer, w.appellation, w.vintage, w.price, w.abv,
             w.soil, w.notes, twin, wine_id), one=True)
    refresh_state(event_id)
    return row


@app.delete("/api/host/wines/{wine_id}", dependencies=[Depends(require_host)])
def delete_wine(wine_id: str):
    cur = q("select * from wines where id = %s", (wine_id,), one=True)
    if not cur:
        raise HTTPException(404, "No such wine")
    ev = get_event(str(cur["event_id"]))
    if not round_is_editable(ev, cur["round_no"]):
        raise HTTPException(409, "This wine's round has started, so it can't be removed")
    q("update wines set twin_of = null where twin_of = %s", (wine_id,))
    q("delete from wines where id = %s", (wine_id,))
    refresh_state(str(cur["event_id"]))
    return {"ok": True}


# ---------- host: running the tasting ----------

class ActionIn(BaseModel):
    action: str = Field(pattern="^(open|lock|reopen|reveal|next_round|finish)$")


@app.post("/api/host/events/{event_id}/action", dependencies=[Depends(require_host)])
def run_action(event_id: str, body: ActionIn):
    ev = get_event(event_id)
    st, rnd, n = ev["status"], ev["current_round"], round_count(event_id)
    wines_in_round = q("select count(*) as n from wines where event_id = %s and round_no = %s",
                       (event_id, rnd), one=True)["n"]
    a = body.action
    if a == "open" and st == "setup":
        if not wines_in_round:
            raise HTTPException(409, f"Add at least one wine to round {rnd} first")
        new, step = ("open", rnd), None
    elif a == "lock" and st == "open":
        new, step = ("locked", rnd), None
    elif a == "reopen" and st == "locked":
        new, step = ("open", rnd), None
    elif a == "reveal" and st == "locked":
        new, step = ("revealing", rnd), 0
    elif a == "next_round" and st == "revealing" and rnd < n:
        new, step = ("open", rnd + 1), 0
    elif a == "finish" and st == "revealing" and rnd == n:
        new, step = ("done", rnd), None
    else:
        raise HTTPException(409, f"Can't {a.replace('_', ' ')} while the tasting is {st} (round {rnd} of {n})")
    q("update events set status = %s, current_round = %s where id = %s", (*new, event_id))
    return refresh_state(event_id, reveal_step=step)


@app.post("/api/host/events/{event_id}/reveal/{direction}", dependencies=[Depends(require_host)])
def step_reveal(event_id: str, direction: str):
    ev, rounds = all_rounds(event_id)
    if ev["status"] != "revealing":
        raise HTTPException(409, "Start the reveal first")
    cur = rounds[ev["current_round"] - 1]
    step = max(0, min(len(cur["steps"]), cur["shown"] + (1 if direction == "next" else -1)))
    refresh_state(event_id, reveal_step=step)
    return {"reveal_step": step, "total_steps": len(cur["steps"])}


@app.get("/api/host/events/{event_id}/reveal", dependencies=[Depends(require_host)])
def host_reveal(event_id: str):
    """Everything, including steps not yet shown, so the host can see what's coming."""
    ev, rounds = all_rounds(event_id)
    return {"status": ev["status"], "current_round": ev["current_round"], "rounds": rounds}


@app.get("/api/host/members", dependencies=[Depends(require_host)])
def host_members():
    return q("select id, name from members order by name")


@app.get("/api/host/events/{event_id}/scores", dependencies=[Depends(require_host)])
def host_scores(event_id: str):
    """Every score in the event, for the paper-entry grid and progress list."""
    return q("""select s.wine_id, s.member_id, s.appearance, s.aroma, s.taste, s.aftertaste,
                       s.overall, s.total, s.price_guess, s.note, s.entered_by
                from scores s where s.event_id = %s""", (event_id,))


@app.put("/api/host/scores", dependencies=[Depends(require_host)])
def host_score(s: ScoreIn):
    """Paper sheets. The host can enter categories or just a total, until the reveal starts."""
    return save_score(s, "host", allow_total_only=True)


@app.delete("/api/host/scores/{event_id}/{wine_id}/{member_id}", dependencies=[Depends(require_host)])
def delete_score(event_id: str, wine_id: str, member_id: str):
    ev = get_event(event_id)
    wine = q("select round_no from wines where id = %s", (wine_id,), one=True)
    if not wine or wine["round_no"] != ev["current_round"] or ev["status"] not in ("open", "locked"):
        raise HTTPException(409, "Only scores in the round being tasted can be removed")
    q("delete from scores where wine_id = %s and member_id = %s", (wine_id, member_id))
    refresh_state(event_id)
    return {"ok": True}


@app.get("/api/host/events/{event_id}/export.csv", dependencies=[Depends(require_host)])
def export(event_id: str):
    rows = q("""
      select e.held_on, e.name as event, w.round_no, w.pour_no, w.name as wine, w.producer,
             w.appellation, w.vintage, w.price, w.abv, w.soil, m.name as member,
             s.appearance, s.aroma, s.taste, s.aftertaste, s.overall, s.total,
             s.price_guess, s.note, s.entered_by
      from scores s join wines w on w.id = s.wine_id join members m on m.id = s.member_id
      join events e on e.id = s.event_id
      where s.event_id = %s order by w.round_no, w.pour_no, m.name""", (event_id,))
    buf = io.StringIO()
    if rows:
        wr = csv.DictWriter(buf, fieldnames=rows[0].keys())
        wr.writeheader()
        wr.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="tasting-{event_id}.csv"'})
