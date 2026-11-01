# Notice review pack: $title

Source: $source. Policy pack: `$policy`. Prepared: $date by $prepared_by.

> This pack asks a lawyer to review the wording of a privacy notice. dpdpkit records and enforces the notice
> as configured. It does not decide whether the wording is lawful, and nothing in this pack is legal advice.
> The automated checks below test the structure of the notice, not its legal sufficiency.

## Summary

$summary

## What the reviewer is asked to do

1. Read the notice in every locale under "The notice as principals will see it".
2. Answer the questions for counsel, and note any change to the wording.
3. Sign off below. An approval covers only the exact wording with the content hashes listed. Any later edit
   changes the hash and needs a fresh review.

## Sign-off

The hashes below are the `content_hash` dpdpkit records in the ledger (`notice.published`) when this wording is
published, so this signed pack can be matched to the exact published version.

$hashes

| Decision | Reviewed by | Firm | Date | Signature |
| --- | --- | --- | --- | --- |
| Approved / approved with the changes noted / not approved | | | | |

Changes requested:

&nbsp;

## Automated checks

$automated

## Questions for counsel

$questions

## Notes from the configuration

$notes

## The notice as principals will see it

Each purpose shows its id, whether it is required or optional, and its legal basis. In the `@dpdpkit/react`
`<ConsentNotice>` widget every consent purpose has its own checkbox, unticked until the principal chooses, and
required purposes carry a "Needed to provide the service" badge. Purposes that rest on a legitimate use (no
consent asked) are listed by title and personal data under "No consent needed", without their description.

$notices
