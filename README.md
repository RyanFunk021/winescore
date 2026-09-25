# Wine tasting app

Phones score the flight, the host runs a synchronized reveal, and the analytics
hand out awards. FastAPI on Render, Postgres + Realtime on Supabase.

## Screens
- **Guests: `/`** (or `/?code=NOV` from the QR code). Pick your name once, score by
  category, add an optional price guess and note, watch the reveal on your phone.
- **Host: `/host`**, protected by your PIN. Four tabs:
  - **Run**: join code and QR code, who's finished, and one button for the next step:
    open scoring, close scoring, start the reveal, show next, open next round, finish.
    Shows what's on everyone's phone now and what comes next.
  - **Flight**: plan the tasting ahead of time. Add wines with producer, appellation,
    vintage, price, ABV, soil and reveal notes; assign each to a round; mark a twin pour.
  - **Paper**: pick a person (or add someone new) and enter their sheet, by category or
    as a total only. Works while scoring is open or closed, until the reveal starts.
  - **Results**: ranking and awards per round, and a CSV download for the club spreadsheet.

## Rounds
Any number of wines, split into any number of rounds: three and a reveal, six and a
reveal, or 3 + 3 + a dessert wine. Each round is scored, closed and revealed on its
own. Wine awards (best nose, best finish, value pick) come with each round; palate
awards (Oracle, Contrarian, most generous, toughest judge, steadiest palate, best
price guesser) close the last round, judged across every wine that night.

Rules that protect the results:
- Guests only see pour numbers until the reveal.
- Pour numbers and rounds are fixed once a round's scoring opens; names, prices and
  notes can still be corrected.
- Scores freeze when a round's reveal starts.

## Setup
1. **Supabase**: SQL editor, run `supabase/schema.sql`. (If you ran the earlier version,
   run `supabase/migrations/002_rounds.sql` instead.)
2. **Render**: New, Blueprint, point at this repo (uses `render.yaml`). Set:
   - `DATABASE_URL`: Supabase, Connect, Transaction pooler string (port 6543)
   - `SUPABASE_URL`, `SUPABASE_ANON_KEY`: Supabase, Project Settings, API
   - `HOST_PIN`: your host PIN
3. Open `/host`, plan the tasting, and print or show the QR code at the table.

API docs for everything the screens use: `/docs`.

## Local run
    pip install -r requirements.txt
    DATABASE_URL=postgresql://... HOST_PIN=1234 uvicorn app.main:app --reload

## Later: the club hub
Newsletter, photos, upcoming tastings, treasurer's report, and season standings, so
the business happens in the app and the evening is about the wine. Members and events
are already in the database, so these build on what's here.
