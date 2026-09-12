# Concept-first interview packet

Build the complete private packet before asking the first question. The packet
contains six to eight broad conceptual themes, normally seven. It is the stable
conceptual answer key and probe plan for the round.

## Packet header

Record the target role, category, seniority, job, resume, project, topic,
sources used, selected themes and why they matter, user-requested constraints,
and whether a quantitative probe is justified.

For a broad ML engineering round, choose different themes across model
behavior, data and evidence, implementation, production, and engineering
tradeoffs. Do not split one family such as precision, recall, thresholds, and
imbalance into several nominal themes.

## Theme card

Create one complete card per theme:

```markdown
## Theme 1 — <concept family>

- Grounding: <job requirement, resume claim, requested topic, or extension>
- Why it matters: <connection to the target>
- Conceptual opening: <exact candidate-facing question>
- Reference model: <short explanation of the underlying mechanism>
- Concept nodes: <essential ideas>
- Required relationships: <cause, constraint, contrast, or implication>
- Important boundaries: <where the model stops applying>
- Acceptable alternatives: <other valid explanations>
- Material misconceptions: <errors that change predictions or decisions>
- Specific probe A: <prepared transfer question>
- Probe A solution: <answer and conceptual connection>
- Specific probe B: <alternative probe for a different observed gap>
- Probe B solution: <answer and conceptual connection>
- Optional probe C: <only when a different branch is useful>
- Evidence status: unasked
- Candidate evidence: <fill during interview>
- Assessment gap: <fill during interview>
```

The reference model should express a compact causal structure: what changes,
why it changes, what result follows, and which assumptions are required.

## Question and probe checks

A focused conceptual opening identifies the relationship to explain. It should
not ask for everything about a topic or make arithmetic the task. A later probe
may ask the candidate to predict a changed outcome, trace a small workflow,
choose between approaches using stated constraints, diagnose a failure, apply
the concept to a project claim, or distinguish two hypotheses with evidence.

Prepare two or three probes and ask at most one. Do not ask a second probe to
repair a failed first probe or retest an answer that was just revealed.

Before the interview, confirm that the themes do not overlap, each opening is
focused and conceptual, every probe tests transfer of its opening concept,
solutions explain mechanisms and boundaries, and trivia, puzzles, formula
recall, and unnecessary calculations have been removed.
