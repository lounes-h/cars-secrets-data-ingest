import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

/** Marks or unmarks a lot as a favorite of the signed-in user. In a clickable row it does not
 * open the lot. `labelled` adds the words, for the lot page. */
export function FavoriteButton({ source, lotNumber, favorite, labelled = false }: {
  source: string; lotNumber: string; favorite: boolean; labelled?: boolean;
}) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (favorite ? api.removeFavorite : api.addFavorite)(source, lotNumber),
    onSettled: () => Promise.all([
      qc.invalidateQueries({ queryKey: ["lots"] }),
      qc.invalidateQueries({ queryKey: ["lot", source, lotNumber] }),
    ]),
  });
  const label = favorite ? "Remove from favorites" : "Add to favorites";
  return (
    <button className={`favorite${favorite ? " on" : ""}${labelled ? "" : " icon-only"}`} aria-pressed={favorite}
      aria-label={labelled ? undefined : label} title={toggle.error?.message ?? label} disabled={toggle.isPending}
      onClick={(e) => { e.stopPropagation(); toggle.mutate(); }}>
      <span aria-hidden="true">{favorite ? "★" : "☆"}</span>
      {labelled && (favorite ? " Favorite" : " Add to favorites")}
    </button>
  );
}
