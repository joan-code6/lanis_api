# Account preferences hotfix

The UI sends hex appearance colors and secondary_theme_color, while the API accepted named colors only and silently discarded the secondary color. The UI also sends homework.hide_completed_in_overview, which the API discarded. The UI load handler includes the migration PATCH, explaining a 422 reported as a loading failure.

Accept validated hex colors and persist secondary colors and homework visibility, retaining compatibility with named colors and older homework display preferences. HTTP regressions cover save/reload, retaining colors across partial updates, and invalid colors. No production writes or authenticated requests were replayed.
