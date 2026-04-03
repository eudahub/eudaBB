---
name: Security and performance warnings
description: Always warn when user's idea could cause security vulnerability or performance problem
type: feedback
---

Always proactively warn when the user's proposed design could:
- Be a DoS/attack vector (e.g. O(n) Argon2 iterations per request)
- Leak information (e.g. revealing whether email exists in DB)
- Create performance bottlenecks at scale
- Have security implications they may not have considered

**Why:** User explicitly asked for this after the email hash lookup case — iterating 1036 Argon2 hashes per request would be slow AND could be used as a DoS amplification attack.

**How to apply:** Before implementing, if the proposed approach has such risks, state them clearly and suggest the safer/faster alternative. Don't just implement what was asked without flagging the issue.
