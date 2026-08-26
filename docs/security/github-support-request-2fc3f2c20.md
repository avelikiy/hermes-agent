# GitHub Support request — purge commit 2fc3f2c20 from the fork network

**Status:** ready to send. Submit at https://support.github.com/request
(category: *Account or repository* → *Remove sensitive data*), signed in as
the repository owner.

## Why a support request is the only remediation

The commit was force-pushed out of `feat/cost-cascade-router` in
`avelikiy/hermes-agent`, so no branch or pull request references it. It remains
readable by direct SHA — and not only from the fork: GitHub serves objects
across a whole fork network, so it is also reachable through the public parent
`NousResearch/hermes-agent`.

That is why making the fork private does **not** help. Visibility is a property
of the repository; the object lives in the network shared with a public parent.
Only GitHub can drop it.

## Request text

> Hello,
>
> I need a commit purged from a repository network. It was pushed to my fork
> and has since been removed from every branch by a force-push, but it is still
> retrievable by its SHA — including through the public parent repository.
>
> Commit: `2fc3f2c207e259949e5e99c8fffcd08824931279`
> Pushed to: `avelikiy/hermes-agent` (branch `feat/cost-cascade-router`)
> Also reachable via: `NousResearch/hermes-agent` (parent of the fork)
>
> The commit message describes unremediated security weaknesses of a live
> personal deployment. It contains no credentials, but it should not be public.
> No branch or pull request references the commit any more.
>
> Please remove the commit and any cached views of it from the network.
>
> Thank you.

## After it is confirmed removed

Verify with:

    gh api repos/NousResearch/hermes-agent/commits/2fc3f2c20 --jq .sha

A `404` means it is gone. While it still returns the SHA, it is still public.
