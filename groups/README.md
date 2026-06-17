# Groups

A **group inbox**. Each `groups/<slug>/` folder is one PI group (the slug is the
PI-table key, e.g. `bethge`).

**To assign a paper to a group, drop its `.bib` into that group's folder** and open a
merge request. The bot files the paper canonically under `entries/<year>/<citekey>.bib`,
unions `<slug>` into the entry's sidecar `custom.groups`, and deletes your drop — these
folders hold nothing between contributions. No JSON or git-layout knowledge needed; pick
the folder from the existing list rather than inventing a new slug.

See the [repository README](../README.md#assign-a-paper-to-a-group) for the full
convention (including the three outcomes of a drop and how to assign several groups at
once). `custom.groups` in the sidecar is the single source of truth for membership; these
folders are an input convenience. To remove a group, edit the sidecar — don't just delete
a file.
