# Colleague trial

This protocol tests the documentation with a person who has not used chronozarr. The question is whether a scientist with their own raster files can get from chronozarr.org to a link that a second person opens, with no help beyond the documentation. As of 2026-10-16 it has not been run with an outside person. The simulated trial of 2026-10-12 is in the [measurement log](evidence.md#newcomer-trial-2026-10-12).

## People

| Role | Who |
|---|---|
| Tester | A scientist who works with raster data and has not used chronozarr. Not a chronozarr contributor |
| Recipient | A second person, on another computer and another network |
| Observer | A chronozarr contributor who watches and takes notes. They do not help unless the tester is stuck for 5 minutes |

## Before the session

1. Ask the tester to bring 5 to 50 dated rasters of their own on one grid, as GeoTIFFs, a NetCDF file or a Zarr store. Do not check or prepare the files.
2. Use the tester's own computer, with whatever Python they have. Do not install anything for them.
3. Start a screen recording if the tester agrees. Note the date, the operating system, the browser and the chronozarr version that gets installed.

## Tasks

Give the tester these tasks one at a time, in writing. Start each one at https://chronozarr.org.

1. "Make your files into something you can look at in your browser."
2. "Send a link to the recipient so that they can look at it too. Show them a date and a place that you choose."
3. "Get the values of one pixel over time in Python, or in the tool you normally use."
4. Optional, if the tester has a cloud bucket: "Make a link that keeps working after you close your laptop."

Ask the recipient to open the link, to say what they see, to move to another date and to click a pixel.

## What to record

For every task:

| Item | Note |
|---|---|
| Start and end time | Also the time to the first useful view |
| Pages opened | In order |
| Commands run | Copy them from the terminal |
| Every error message | Verbatim, with what the tester did next |
| Hesitations | Every place where the tester stopped, guessed or asked |
| Help given | What the observer said, and after how long |
| Did the docs predict it? | Yes, no or partly, for each step |
| Recipient | Browser, time to the first view, whether the chosen date and place arrived |

## After the session

Ask the tester:

1. What did you expect chronozarr to do, before you started?
2. Which step took longest, and why?
3. Was there a moment when you did not know what to do next?
4. Would you use this to share data with a colleague? What would stop you?

Write the record into the measurement log with the date, and open an issue for each problem that a change can fix.

## Analysis

This section was fixed on 2026-10-16, before the first baseline session. Any later change is listed here with its date and reason.

| Item | Definition |
|---|---|
| Primary endpoint | Minutes from the tester opening https://chronozarr.org to the recipient's first complete frame of the tester's own data (tasks 1 and 2) |
| Interventions | Count, and the minute of each |
| Values | Task 3 passes when the values of one pixel over time equal the source files |
| Recipient | Whether the chosen date and place arrived |
| Give-up | The tester stops, or 45 minutes pass with no link opened by the recipient |
| Target | 5 of 5 testers reach a link the recipient opens, with no intervention, median 10 minutes or less |

A session stops at 45 minutes or when the tester asks to stop. A session goes on after an intervention, and that tester does not meet the target. Give-ups are censored at the minute they stop. The record gives each tester's time, the median from a Kaplan–Meier estimate and the intervention count. Five testers allow no significance test, so the comparison of the baseline with later sessions is descriptive.

The baseline is every session run with chronozarr 0.4.1 before the first change that a session prompts. Problems found in the baseline are fixed only after the baseline is in the measurement log.

## Simulated newcomers

Simulated sessions are a cheaper check and do not replace people. An agent from another model family, with no project context, plays the tester. It reads only chronozarr.org and runs commands only in a fresh Linux container built by `scripts/trial/`. A second fresh container with a headless browser plays the recipient. The tasks, endpoints and limits are those above.

The container's package index holds only packages released at least 7 days earlier, plus chronozarr built from its release tag. A package outside that index fails to install, and the record marks such a failure as a limit of the setup. An agent does not skim or hesitate as a person does, so a simulated time is a lower bound for steps the documentation covers and does not show where a person gets lost. Simulated records are kept apart from sessions with people.
