# Security policy

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private vulnerability
reporting on this repository — the *Security* tab, then *Report a
vulnerability*. That gives us a private thread and a CVE path if one is needed.

Tell us what you can: what you did, what happened, and what you expected. A
proof of concept helps and is not required. If you are unsure whether something
is a vulnerability, report it anyway — deciding that is our job, not yours.

We aim to acknowledge within 3 working days. This is a small project, not a
vendor with an on-call rota, and saying so is more useful than a number we
would miss.

## Scope

In scope: authentication and authorization, the gate and attestation path,
anything that lets a participant act as another actor, secrets reaching a place
they should not, and remote code execution.

**Out of scope, because it is the design.** This software executes procedures
by running commands and by handing work to AI agents. A gate's `check` is a
shell command by definition, and an agent given a task can do what an agent can
do. The security boundary is around *who may write those instructions*, not
around what a command can do once written. Reports that an executed command
executes are not findings; reports that the wrong party got to choose one are.

## What we do with reports

Fixed issues get a test that fails against the commit before the fix, and the
reasoning goes in the commit message rather than a changelog line. Accepted
risks are written down in `docs/known-issues.md` with the argument for
accepting them, so an adopter can disagree with us on the evidence.

## Supported versions

Pre-1.0 and moving. Only the default branch is supported. There is no security
backport channel yet; if you need one, say so in a report and we will talk.
