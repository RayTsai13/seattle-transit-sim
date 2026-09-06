import { MapboxOverlay } from '@deck.gl/mapbox';
import type { MapboxOverlayProps } from '@deck.gl/mapbox';
import { useControl } from 'react-map-gl/maplibre';
import { useEffect } from 'react';

export function DeckGLOverlay(props: MapboxOverlayProps & { interleaved?: boolean }) {
  const overlay = useControl<MapboxOverlay>(() => new MapboxOverlay(props));
  
  useEffect(() => {
    overlay.setProps(props);
  }, [overlay, props]);

  return null;
}
