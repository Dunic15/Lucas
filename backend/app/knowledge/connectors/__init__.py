"""Source connectors for the Company Brain data plane.

`base` defines the source-agnostic contract every connector implements;
`msgraph` is the Microsoft Graph adapter (transport-injected); `fake_graph`
is the fully synthetic, key-free Graph tenant used by tests and the offline
demo. Google Drive / Slack / Notion / Confluence / Box adapters slot in here
without touching the retrieval core (docs/company-brain/CONNECTOR-ROADMAP.md).
"""
