# Weekly custom EPG for TiviMate

This repository hosts the filtered XMLTV guide used by the custom TiviMate playlist.

**TiviMate EPG URL:** https://raw.githubusercontent.com/zacmorgan/tivimate-epg/main/all-channels-epg.xml.gz

The GitHub Actions workflow refreshes the guide every Sunday at **16:23 UTC** (10:23 a.m. in Regina during CST) and can also be run manually from the **Actions** tab. It downloads the current U.S., Canadian, U.K., Mexican, Argentine, and Uruguayan guides from [IPTV-EPG.org](https://iptv-epg.org/guides), keeps only the channel IDs listed in `custom-channels.json`, validates the complete result, and updates `all-channels-epg.xml.gz` on `main`.

The workflow stops without replacing the existing guide if a source download fails, the XML is invalid, or any selected guide ID has no programme listings. TiviMate can continue using the previous valid file in those cases.

GitHub may delay scheduled jobs during busy periods. Public-repository schedules can also be disabled after 60 days without repository activity; the workflow can be re-enabled and run manually from **Actions**.
