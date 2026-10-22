# The NeuroRadVault catalogue

This folder is the project's own dataset catalogue. The app reads it from this repository, cached for 24 hours,
and uses the copy bundled with the app when offline.

| File | What it is | Who updates it |
|---|---|---|
| `Neoplasm.csv`, `Cerebrovascular.csv`, ... | One CSV per domain (22 metadata columns) | The monthly *Update catalog* workflow mirrors them unchanged from an upstream registry. If that source is unreachable or disappears, the last copy is kept. |
| `additions.csv` | Datasets this project adds itself (same columns, plus `category`) | Us, by pull request |
| `../enrichment.json` | Download route, real size and file count for every dataset | The monthly workflow |

## Adding a dataset

Add a row to `additions.csv`. Set `category` to one of: Neoplasm, Cerebrovascular, Neurodegenerative, Spinal,
Neurodevelopmental, Psychiatric, Other. Fill in at least `dataset_name`, `url`, `access_type` (`Open` or
`Restricted (...)`), `modality` and `segmentation_mask` (`Yes ...` / `No`). If the link is on OpenNeuro, Zenodo,
Figshare, Mendeley Data, Hugging Face, OSF or a public TCIA collection, the dataset becomes one-click
automatically.

## Licence of the mirrored files

The domain CSVs are an unmodified copy of the NeuroAIHub database
(<https://github.com/NeuroAIHub-Registry/NeuroAIHub>), © the NeuroAIHub team, licensed CC BY-NC-ND 4.0. They are
redistributed unchanged, non-commercially, with this attribution, as that licence requires.
