# TERRY'S LEARNING LOG

Plain-English lessons from the work we do together. Each one is short. Reread them
any time. New ones get added at the bottom as they come up.

---

## 1. The page you edit and the machine that writes the page

**The comparison:** a bakery's recipe card versus the loaf. If you fix the loaf, the
next batch comes out the old way. Fix the card and every loaf is right.

**What it means here:** most pages on our sites are written by a program every
half hour from a set of instructions (`wire.py` on Newswire, `render_site.py` on
Gospel). Those are the recipe cards. The `.html` files are the loaves.

**What went wrong:** on the CDC pages I fixed the loaf. Thirty minutes later the
program baked a fresh one and my fix vanished. The fix has to go into the recipe.

**The word:** *source* is the recipe. *Output* or *generated* is the loaf.

## 2. Draft versus live: branches and main

**The comparison:** a Word document you are still editing versus the one you have
sent to the printer.

**What it means here:** a *branch* is a private draft copy of the whole site. *main*
is the published version, the only one the world sees. *Merging* is sending the
draft to the printer. That is why we could show you the Freda Payne review privately
and only publish it when you said go.

## 3. Why a photo gets its face cut off

**The comparison:** a tall picture pushed into a wide frame, like fitting a portrait
into a letterbox. Something has to be trimmed.

**What it means here:** the front page shows photos in a wide banner and trims from
the middle. Her face was near the top, so it went. The fix was a wide crop with her
face inside it, plus a setting called a *focal point* that says which part of the
photo to protect.

**The word:** *cropping* is trimming a picture to fit a frame. `object-fit: cover`
is the instruction that says fill the frame and trim the rest.

## 4. Linking to a photo versus copying it

**The comparison:** pointing at a painting in a museum versus taking it home.

**What it means here:** for wire stories we point at the publisher's own photo and
print their name under it. We do not copy it onto our server. That keeps us on the
right side of copyright, and it is why every wire photo now says "Photo: [outlet]".
The word for pointing at someone else's file is *hotlinking*.

## 5. Telling Google and Bing you exist

**The comparison:** handing the post office a change-of-address card instead of
waiting for the mail to find you.

**What it means here:** a *sitemap* is a list of every page on your site, handed to
search engines. A *news sitemap* is the same list for fresh stories only, which is
what Google News reads. *IndexNow* is a shortcut that tells Bing and others the
moment a page changes. None of these can promise a ranking. They only make sure
you are found.

## 6. Testing with a pretend internet

**The comparison:** a fire drill. You practise with a fake fire so the real one goes
right.

**What it means here:** my workspace cannot reach the live internet. So when I wrote
the code that hunts for missing photos, I tested it against a pretend internet that
I controlled, with a good page, a bare page and a YouTube link. It passed every case
before it ever touched a real website. That is called a *test with a stand-in*, or
a *mock*.

## 7. "Nothing changed" versus "something new"

**The comparison:** a guard who checks that every painting in the gallery is still
the same, but never looks at the empty wall where a new painting was just hung.

**What it means here:** the hourly robot asked Git (the tool that tracks every file
version) "did any page *change*?" using a command called `git diff`. That command
only compares files Git already knows. The homepage had never existed, so it was a
brand-new file and Git didn't compare it at all. The answer was always "no change",
and the finished site was thrown away every hour. The fix uses `git status`, which
also sees new files. Lesson: a check can say "all clear" when it isn't looking at
the right thing.
