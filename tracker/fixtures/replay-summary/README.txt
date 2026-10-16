Public competition replay fixtures, downloaded October 16, 2026.

legacy: match 7743f796-0ca5-48f4-8ebd-4ad20ce51d5c
  https://2026a.battlecode.co.uk/api/v1/matches/7743f796-0ca5-48f4-8ebd-4ad20ce51d5c/bundle
  Group ef5526a9-8f78-42ae-abb2-633edd2381c7 provided the expected full inline
  statistics in legacy.json. Those values are independent of our replay decoder.

recent: match 2154025b-1c95-4cd1-a120-b3b64232a19a
  https://2026a.battlecode.co.uk/api/v1/matches/2154025b-1c95-4cd1-a120-b3b64232a19a/bundle
  Group 67c8e148-50d5-4393-a568-5a8d4361cb94 provided the public rounds, winner,
  scores, and draft in recent.json, but no inline statistics.

Each .repl26a.gz contains only the replay.repl26a member of the downloaded ZIP,
compressed with deterministic gzip. No credentials or bot logs are included.
The tests run these replay bytes through the pinned official visualiser WASMs.
incomplete.repl26a.gz is the legacy replay with only its eight draft actions;
it is a structurally valid unfinished replay used to check completion guards.
