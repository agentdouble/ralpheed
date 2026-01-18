Instructions for each iteration:
markdown
# Ralph Agent Instructions

## Your Task

1. Read `{{WORKSPACE_ROOT}}/AGENTS.md` if it exists, otherwise `{{CONTROL_ROOT}}/AGENTS.md`
2. Read `{{PRD_PATH}}`
3. Read `{{CONTROL_ROOT}}/progress.txt`
   (check Codebase Patterns first)
4. Work in the current repo (workspace path)
5. Check you're on the correct branch
6. Pick highest priority story 
   where `passes: false`
7. Implement that ONE story
8. Run typecheck and tests
9. Update `{{WORKSPACE_ROOT}}/AGENTS.md` if it exists, otherwise `{{CONTROL_ROOT}}/AGENTS.md`
10. Commit: `feat: [ID] - [Title]`
11. Update `{{PRD_PATH}}`: `passes: true`, `status: review`
12. Append learnings to `{{CONTROL_ROOT}}/progress.txt`

## Progress Format

APPEND to {{CONTROL_ROOT}}/progress.txt:

## [Date] - [Story ID]
- What was implemented
- Files changed
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
---

## Codebase Patterns

Add reusable patterns to the TOP 
of progress.txt:

## Codebase Patterns
- Migrations: Use IF NOT EXISTS
- React: useRef<Timeout | null>(null)

## Stop Condition

If ALL stories pass, reply:
<promise>COMPLETE</promise>

Otherwise end normally.
