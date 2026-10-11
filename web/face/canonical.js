import data from './canonical-data.js';
import { hydrateRig } from './rig-data.js';
/** A fresh rig from baked data. No fitting or rig construction takes place during playback. */
export const canonicalRig = () => hydrateRig(data);
