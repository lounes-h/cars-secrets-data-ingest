import { useState, type KeyboardEvent } from "react";
import type { LotImage } from "../lib/api";

/** Main photo with previous/next and a strip of thumbnails. Photos load straight from the
 * provider's image host (allowed by the CSP); no referrer is sent. */
export function Gallery({ images, alt }: { images: LotImage[]; alt: string }) {
  const [index, setIndex] = useState(0);
  if (images.length === 0) return <div className="gallery-empty muted">No photos for this lot.</div>;

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
    </div>
  );
}
