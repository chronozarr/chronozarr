# Colleague trial

This protocol tests the documentation with a person who has not used chronozarr. The question is whether a scientist with their own raster files can get from chronozarr.org to a link that a second person opens, with no help beyond the documentation. As of 2026-10-12 it has not been run with an outside person. The simulated trial of 2026-10-12 is in the [measurement log](evidence.md#newcomer-trial-2026-10-12).

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
