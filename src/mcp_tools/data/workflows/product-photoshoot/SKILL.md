---
name: product-photoshoot
description: Make a set of consistent product photos from one photo of the product - clean cutout, hero shot, lifestyle scenes and detail shots.
version: "1"
---
# Product photoshoot

Use this when the user has a photo of a real product and wants marketing pictures of it.
The product must stay the same object in every picture: its shape, colours, label and logo
are not to be reinvented.

## 1. Get the product photo in

- A link: `media_import_url`. A local file: `media_import_path`, or `media_upload` followed by
  `media_confirm` - use whichever of these this server offers. Keep the returned `media_id`.
- Look at it with `media_get` and `include_preview: true`. If it is blurry, cropped or
  cluttered, say so and ask for a better photo before spending anything.

## 2. Agree on the set

Propose 4 to 6 pictures and let the user edit the list. A good default (see `scenes.md`):
one clean cutout, one hero shot on a plain backdrop, two lifestyle scenes, one detail shot.
Ask where the pictures will be used: that decides the aspect ratio (square for marketplaces,
4:5 or 9:16 for social, 16:9 for banners).

## 3. Prepare

1. `remove_background` with the product's `media_id` gives the cutout (transparent PNG).
   It is also the first deliverable.
2. `models_explore`, action `recommend`, type `image`, input `media`: choose a model that
   accepts a reference image, then read it with action `get` to learn its media role.
3. `projects_create` for the shoot; pass its `root_folder_id` as `params.folder_id` below.
4. `estimate_cost` for the whole set and tell the user the total.

## 4. Shoot

One `generate_batch`: one request per scene, each with the product photo in `medias` (use the
cutout if the original background is distracting) and a prompt of the form

> Keep the product exactly as in the reference image: same shape, colours, label and logo.
> <scene>. <lighting>. <camera>. Photorealistic product photography.

Wait with `jobs_wait`, then check every result with `media_get` + `include_preview: true`.
Reject and redo, without asking, any picture where the label text is garbled, the logo has
changed, or the product's proportions are off. Then show the set.

## 5. Finish

- Offer `upscale_image` for the pictures the user picks for print or large banners.
- List the final files with their `local_path` or `url`.

## Rules

- Never describe the product from imagination when a reference image exists; point at the
  reference instead.
- `approval_required` means nothing was created: tell the user the estimate and wait.
- Failed or moderated jobs are refunded; change the scene wording and retry that picture only.
