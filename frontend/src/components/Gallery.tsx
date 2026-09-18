import { useState, type KeyboardEvent } from "react";
import type { LotImage } from "../lib/api";

/** Main photo with previous/next and a strip of thumbnails. Photos load straight from the
 * provider's image host (allowed by the CSP); no referrer is sent. */
export function Gallery({ images, alt, complete = true, moreUrl }: {
  images: LotImage[];
  alt: string;
  /** False when the provider's photo list was unavailable and only the export's thumbnail is shown. */
  complete?: boolean;
  /** The lot's page on the auction site, offered when photos are missing. */
  moreUrl?: string | null;
}) {
  const [index, setIndex] = useState(0);
  const more = !complete && moreUrl ? (
    <div className="gallery-more small">
      <span className="muted">
        {images.length ? "Only this photo is available here." : "No photos available here."}
      </span>{" "}
      <a href={moreUrl} target="_blank" rel="noopener noreferrer">See all photos on the auction site ↗</a>
    </div>
  ) : null;
  if (images.length === 0) {
    return (
      <div className="gallery">
        <div className="gallery-empty muted">No photos for this lot.</div>
        {more}
      </div>
    );
  }

  const i = Math.min(index, images.length - 1);
  const current = images[i];
  const main = current.full ?? current.hd ?? current.thumb ?? "";
  const go = (delta: number) => setIndex((i + delta + images.length) % images.length);
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft") go(-1);
    if (e.key === "ArrowRight") go(1);
  };

  return (
    <div className="gallery" tabIndex={0} onKeyDown={onKey} aria-label="Lot photos">
      <div className="gallery-main">
        <img src={main} alt={`${alt}, photo ${i + 1} of ${images.length}`} referrerPolicy="no-referrer" />
        {images.length > 1 && (
          <>
            <button className="gallery-nav prev" aria-label="Previous photo" onClick={() => go(-1)}>‹</button>
            <button className="gallery-nav next" aria-label="Next photo" onClick={() => go(1)}>›</button>
          </>
        )}
        <span className="gallery-count">{i + 1} / {images.length}</span>
        {current.hd && (
          <a className="gallery-hd" href={current.hd} target="_blank" rel="noopener noreferrer">Open HD</a>
        )}
      </div>
      {images.length > 1 && (
        <div className="gallery-strip">
          {images.map((img, n) => (
            <button key={n} aria-label={`Photo ${n + 1}`} aria-pressed={n === i} onClick={() => setIndex(n)}>
              <img src={img.thumb ?? img.full ?? ""} alt="" loading="lazy" referrerPolicy="no-referrer" />
            </button>
          ))}
        </div>
      )}
      {more}
    </div>
  );
}
