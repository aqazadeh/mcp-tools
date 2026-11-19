---
name: storyboard-video
description: Turn an idea into a multi-shot video - plan the shots, generate a still for each, get the user's approval, then animate the stills.
version: "1"
---
# Storyboard video

Use this when the user wants a video with more than one shot (an ad, a short story, a product
film). The method keeps the look consistent and avoids paying for video before the user has
seen what it will look like.

## 1. Plan (no tools)

Write a shot list of 3 to 6 shots. For each: what is seen, camera framing and movement, and
length in seconds. Fix the things that must not change between shots (character, product,
palette, lighting, aspect ratio) in one "style line" you will reuse word for word in every
prompt. Show the list to the user and adjust it until they agree. See `shot-list-template.md`.

## 2. Pick models and price it

1. `models_explore` with action `recommend` and `query` set to the goal, type `image`; then the
   same with type `video` and input `media`
   (the video model must accept a start image). Read each chosen model with action `get`.
2. `estimate_cost` with one request per still and one per clip. Tell the user the total.
3. `projects_create` for the film; keep its `root_folder_id` and pass it as `params.folder_id`
   in every generate call below.

## 3. Stills first

Send all stills in one `generate_batch` (one request per shot; `index` = shot number; the
style line plus the shot description as the prompt; the film's aspect ratio). Wait with
`jobs_wait`. Look at each result yourself with `media_get` and `include_preview: true` before
showing it: regenerate a still that breaks continuity instead of presenting it.

If the same character or product must appear in several shots, generate one reference still
first and pass it as a `medias` entry (by `media_id`) to the other stills, using a model that
accepts reference images.

Show the stills in shot order and ask the user to approve or change them. Do not go on to
video without an explicit yes: video is the expensive step.

## 4. Animate

One `generate_batch` with a video request per approved still: `medias` holds the still as the
model's start-image role, the prompt describes only motion and camera (the still already
fixes the look), `duration` is the planned length. Pass `max_credits` equal to the estimate
the user agreed to. Wait with `jobs_wait` in rounds; video takes minutes.

## 5. Deliver

List the clips in shot order with their `local_path` or `url`. This server does not join
clips: say so, and give the order and the lengths so the user can assemble them in an editor.
Offer to redo single shots; never regenerate everything for one weak shot.

## Rules

- If a generate call answers `approval_required`, nothing was created: tell the user the
  estimate and wait.
- A `moderated` or `failed` job is refunded. Report its `error.message`, change the prompt,
  and retry that shot only.
- Reuse the same `idempotency_key` when retrying a call after a timeout.
