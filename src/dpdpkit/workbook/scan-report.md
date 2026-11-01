# Personal data scan report: $client

Readiness sprint. Workshop: $workshop_date. Report: $date. Prepared by: $prepared_by.

> This report lists the kinds of personal data found, where they are and how often they matched. It never
> contains the sampled values. It records engineering findings, not legal advice: purposes, legal bases,
> retention periods and notice wording are decisions for the client and their counsel.

## Summary

$summary

## Scope and method

$stores

Each data store marked as scanned was read over a read-only connection, or from a copy of its files. A capped
number of rows per column was sampled and matched against recognisers for Indian identifiers (Aadhaar with its
checksum, PAN, GSTIN, IFSC, UPI ID, mobile number, voter ID, passport, vehicle registration, ABHA) and common
personal data (names, email addresses, dates of birth, addresses), together with column-name heuristics.
Sampled values were held in memory only and were not written to disk, logs or this report.

**Limits.** Sampling can miss rare values. Free-text columns, images, scanned documents and stores not marked as
scanned may hold personal data this report does not show. Confidence is the recogniser's, not a legal
classification.

## Findings by data store

$findings

## Personal data not in the registry

These locations hold personal data that no registered data item covers. Each one needs a decision: register it
under a purpose with a retention rule, delete it, or minimise it (mask, truncate or hash).

$unregistered

## Registered data items not found by the scan

These data items are in the registry but no scan finding maps to them. They may live in a store that was not
scanned, in a format the recognisers missed, or no longer be collected. Confirm which, and remove items that are
no longer collected.

$not_found

## Low-confidence findings

Listed separately so they do not crowd the findings above. Review each one and mark it as a false positive or map
it to a data item.

$low

## Gap list

Readiness checks with status "gap" or "partly met", highest risk first. Each is mapped to the dpdpkit feature
that addresses it.

$gaps

Purposes awaiting counsel's confirmation: $unconfirmed.

## Next steps

1. Decide on each location in "Personal data not in the registry", and record the decision in the workbook.
2. Have counsel confirm every purpose, legal basis and retention period (Purposes sheet, "Counsel confirmed").
3. Generate the configuration from the workbook:
   `dpdpkit workbook import data-mapping.xlsx --base dpdpkit.yaml --out dpdpkit.yaml`.
4. Work through the gap list with the owners and dates above, then scan again to confirm.

## Sign-off

| Role | Name | Signature | Date |
| --- | --- | --- | --- |
| Client tech lead | $tech_lead | | |
| Compliance owner | $compliance_owner | | |
| Prepared by | $prepared_by | | |
