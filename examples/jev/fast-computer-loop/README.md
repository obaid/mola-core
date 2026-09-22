# Jev bounded decision loop

Jev belongs in the external harness. Give it a typed list of candidate actions,
let it select one, validate that selection, then execute the deterministic side
effect over the Mola action session. Mola does not depend on Jev and does not
choose a model or plan tasks. Keep the loop bounded and retain request IDs so a
transport failure never causes a blind replay.
