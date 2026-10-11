// Map a photo onto the face. MediaPipe's face landmarker (run locally from /face-vendor, installed
// by setup-tools) finds the 468 landmarks; because the face mesh *is* that landmark topology, each
// landmark's position in the photo becomes that vertex's texture coordinate.
let landmarker = null;

async function getLandmarker() {
  if (!landmarker) {
    landmarker = (async () => {
      const vision = await import('/face-vendor/vision_bundle.mjs');
      const fileset = await vision.FilesetResolver.forVisionTasks('/face-vendor/wasm');
      return vision.FaceLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: '/face-vendor/face_landmarker.task', delegate: 'CPU' },
        runningMode: 'IMAGE', numFaces: 1,
      });
    })();
    landmarker.catch(() => { landmarker = null; });
  }
  return landmarker;
}

/** -> {image: JPEG data URL (max 1024 px), landmarks: [[x, y] x 468] (0..1), w, h} */
export async function preparePhoto(file) {
  const bitmap = await createImageBitmap(file).catch(() => { throw new Error("that file isn't a picture I can read"); });
  const s = Math.min(1, 1024 / Math.max(bitmap.width, bitmap.height));
  const w = Math.round(bitmap.width * s), h = Math.round(bitmap.height * s);
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  canvas.getContext('2d').drawImage(bitmap, 0, 0, w, h);
  let lm;
  try { lm = await getLandmarker(); }
  catch { throw new Error('the face finder is not installed (run scripts\\setup-tools.ps1)'); }
  const pts = lm.detect(canvas).faceLandmarks?.[0];
  if (!pts || pts.length < 468) throw new Error("I couldn't find a face in that picture");
  return { image: canvas.toDataURL('image/jpeg', 0.9), landmarks: pts.slice(0, 468).map(p => [p.x, p.y]), w, h };
}
