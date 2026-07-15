# ServiceNow Trading Agent — Capstone

> **Scaffold status:** structure only. The sections marked **`TODO`** need facts that only you
> have — they are left blank deliberately rather than filled with plausible-sounding invention.

## What this is

`TODO` — one paragraph: what the agent does and what problem it solves for the ServiceNow
program.

## Architecture decision: on-platform vs. integrated

This is the fork that determines the entire repo layout, so it should be settled first.

- **On-platform** — a scoped application built in ServiceNow (App Engine, Flow Designer, Business
  Rules, Script Includes). Source lives as XML update sets / a linked repo via Studio. The
  `src/scoped_app/` and `src/flows/` directories assume this path.
- **Integrated** — an external service (Python/Node) that talks to ServiceNow over the Table API,
  MID Server, or IntegrationHub. `src/integration/` assumes this path.

`TODO` — pick one and delete the directories for the other.

## Deliverables and timeline

`TODO` — what the program is actually grading (demo? written report? working app? presentation?),
and the due date.

## Layout

```
docs/                # Design docs, capstone writeup
src/scoped_app/      # ServiceNow scoped application source (if on-platform)
src/flows/           # Flow Designer / IntegrationHub definitions (if on-platform)
src/integration/     # External service + ServiceNow API client (if integrated)
tests/
scripts/             # Update-set import/export, local tooling
```

## Notes

Whatever this agent trades, keep it paper/simulated for a capstone. A graded academic project is
not a reason to connect real capital, and reviewers do not need it to be live to see that it
works.

## License

MIT — see [LICENSE](LICENSE).
