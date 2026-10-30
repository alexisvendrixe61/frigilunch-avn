# Glossary

Dutch and domain-specific terms used throughout the code and documentation.

| Term | Meaning |
|---|---|
| **afsluiten** | To close; `afsluiten_kan` means the product is allowed to be the last of the day; `afsluit_moet` means it is required to be last |
| **allergie_niveau** | Allergen level: an integer 0–6. Levels must run ascending through a production day (a drop requires cleaning). Level 6 is sate (peanut) |
| **antal / aantal** | Quantity — the number of units in an order |
| **art_nr** | Article number — the product identifier shared between the order book and the product master |
| **dagschema** | Day schedule — the built minute-by-minute production timeline for one day |
| **identiek product / identiek_groep** | Identical products — the same recipe in a different pack size or brand variant, linked in the product master. The planner prefers to group them on the same day and line, smallest first |
| **kapvorm** | Lid shape / mold type — the physical form factor of the packaging. Products sharing a kapvorm can be sequenced with shorter changeovers. "NVT" means not applicable (most L2 products) |
| **klant** | Customer |
| **lever_datum** | Delivery date — the date by which the order must reach the customer |
| **lijn** | Production line. L1 (lijn 1) is the automatic packing line; L2 (lijn 2) is the manual packing line |
| **mag_eerst** | May be first — the product is allowed to open the production day |
| **mag_laatst** | May be last — the product is allowed to close the production day |
| **MEP** | Meal-element preparation. Some products require their sauce or filling to be prepared one or two days before production (D-1 or D-2) |
| **moet_eerst** | Must be first — the product is required to open the production day |
| **moet_laatst** | Must be last — the product is required to close the production day |
| **ombouw** | Changeover — the time the line stands still between two consecutive orders while equipment is cleaned, adjusted, or swapped |
| **ombouw_voor** | Changeover before — the changeover minutes that precede this order's production start |
| **opstart** | Start-up / opening; `opstart_kan` means the product may open the day; `opstart_moet` means it must |
| **plan_nr** | Plan number — a unique identifier from the source ERP system, preferred over the composite order identity when available |
| **plan_state** | The JSON file (`data/generated/plan_state.json`) that records committed production days; read by the next run to enforce the rolling freeze |
| **productie_datum** | Production date — the day on which an order is scheduled to be produced |
| **sate** | Peanut-containing products (allergie_niveau = 6). Sate triggers a cross-line halt: while sate runs on one line, the other line may not run any non-sate product. Sate always closes its own line |
| **schaal** | Scale / tray — the physical packaging tray. Products on the same schaal family share changeover head settings and can be sequenced with shorter ombouw |
| **snelheid** | Speed — the production rate in units per hour |
| **toestel** | Machine / device — production equipment such as the Gernal, Leonard, multifill, or teriet. Some machines have only one unit and can only be used by one line at a time |
| **toestellen_beschikbaarheid** | Device availability — the number of units available for each machine type, read from `data/capacity.json`. A machine with ≥ 2 units can never cause a line conflict |
| **vastgezet** | Committed / frozen — an order or day that has been locked by a previous run's commit and cannot be moved |
| **vrij te plannen** | Free to plan — orders that are not yet committed and are available for the current run's solver to assign |
| **werkdag** | Working day — a Monday through Friday production day (weekends are not scheduled) |
