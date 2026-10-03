import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { STLLoader } from './vendor/STLLoader.js';

// All coordinates and transforms come from the generator, in millimetres.
export class Viewer {
  constructor(container) {
    this.container = container;
    this.mode = 'model';
    this.explode = 0;
    this.showHood = true;
    this.entries = [];
    this.lighting = {on: true, brightness: 1, falloff: false};
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#eceee7');
    this.camera = new THREE.PerspectiveCamera(38, 1, .1, 30000);
    this.camera.up.set(0, 0, 1);
    this.renderer = new THREE.WebGLRenderer({antialias: true, alpha: false});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.append(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.minDistance = 5;
    this.controls.maxDistance = 20000;
    this.ambient = new THREE.AmbientLight(0xffffff, 1.6);
    this.scene.add(this.ambient);
    this.key = new THREE.DirectionalLight(0xffffff, 3.2);
    this.key.position.set(-180, -220, 350);
    this.scene.add(this.key);
    this.fill = new THREE.DirectionalLight(0xcad9c8, 1.1);
    this.fill.position.set(100, 120, 150);
    this.scene.add(this.fill);
    this.group = new THREE.Group();
    this.scene.add(this.group);
    this.grid = new THREE.GridHelper(700, 35, 0xc6cfc0, 0xdce2d6);
    this.grid.rotation.x = Math.PI / 2;
    this.grid.position.z = -.15;
    this.grid.material.transparent = true;
    this.grid.material.opacity = .48;
    this.scene.add(this.grid);
    this.marker = new THREE.Mesh(new THREE.SphereGeometry(1.7, 20, 12), new THREE.MeshBasicMaterial({color: 0xffd58e}));
    this.scene.add(this.marker);
    this.marker.visible = false;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.animate = this.animate.bind(this);
    this.animation = requestAnimationFrame(this.animate);
    this.resetCamera();
  }

  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  async setScene(data) {
    const version = this.loadVersion = (this.loadVersion || 0) + 1;
    const loader = new STLLoader();
    const loaded = await Promise.all(data.parts.map(async part => ({part, geometry: await loader.loadAsync(part.url)})));
    if (version !== this.loadVersion) { loaded.forEach(entry => entry.geometry.dispose()); return; }
    for (const entry of this.entries) {
      this.group.remove(entry.mesh);
      entry.mesh.geometry.dispose();
      entry.mesh.material.dispose();
      entry.label.remove();
    }
    this.entries = loaded.map(({part, geometry}) => {
      geometry.computeVertexNormals();
      geometry.computeBoundingBox();
      const color = part.name === 'light_hood' ? 0xb49a70 : part.name === 'base' ? 0x758574 : 0x496451;
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color, roughness: .72, metalness: .06}));
      mesh.matrixAutoUpdate = false;
      this.group.add(mesh);
      const label = document.createElement('div');
      label.className = 'part-label';
      label.textContent = part.label;
      label.hidden = true;
      this.container.append(label);
      return {part, mesh, label, bounds: geometry.boundingBox.clone()};
    });
    this.data = data;
    this.marker.position.fromArray(data.light_position);
    if (this.plane) {
      this.scene.remove(this.plane);
      this.plane.geometry.dispose();
      this.plane.material.uniforms.visibilityMap.value?.dispose();
      this.plane.material.dispose();
    }
    const [x0, y0, x1, y1] = data.projection_bounds;
    const material = new THREE.ShaderMaterial({
      uniforms: {
        visibilityMap: {value: null}, brightness: {value: 1}, lightOn: {value: 1}, falloff: {value: 0},
        source: {value: new THREE.Vector3(...data.light_position)}, reference: {value: (x1-x0)*.25},
      },
      vertexShader: `varying vec2 imageUV; varying vec3 wallPosition;
        void main(){imageUV=uv; wallPosition=(modelMatrix*vec4(position,1.)).xyz; gl_Position=projectionMatrix*viewMatrix*vec4(wallPosition,1.);}`,
      fragmentShader: `uniform sampler2D visibilityMap; uniform float brightness,lightOn,falloff,reference;
        uniform vec3 source; varying vec2 imageUV; varying vec3 wallPosition;
        void main(){float visibility=texture2D(visibilityMap,imageUV).r;
          float distanceToLight=length(source-wallPosition);
          float attenuation=mix(1.,min(2.5,pow(reference/max(distanceToLight,1.),3.)),falloff);
          vec3 dark=vec3(.045,.054,.045); vec3 warm=vec3(.96,.78,.46);
          gl_FragColor=vec4(mix(dark,warm,clamp(visibility*brightness*lightOn*attenuation,0.,1.)),1.);
          #include <colorspace_fragment>
        }`,
      side: THREE.DoubleSide,
    });
    this.plane = new THREE.Mesh(new THREE.PlaneGeometry(x1-x0, y1-y0), material);
    this.plane.position.set((x0+x1)/2, (y0+y1)/2, -data.settings.wall_gap);
    this.plane.visible = this.mode === 'light';
    this.scene.add(this.plane);
    this.arrange();
    await this.setMask(data.mask_url);
    if (version !== this.loadVersion) return;
    this.setLighting(this.lighting);
    this.setMode(this.mode);
  }

  async setMask(url) {
    const plane = this.plane;
    const texture = await new THREE.TextureLoader().loadAsync(url);
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.generateMipmaps = false;
    if (plane !== this.plane) { texture.dispose(); return; }
    plane.material.uniforms.visibilityMap.value?.dispose();
    plane.material.uniforms.visibilityMap.value = texture;
  }

  setLighting(values) {
    Object.assign(this.lighting, values);
    if (!this.plane) return;
    const u = this.plane.material.uniforms;
    u.brightness.value = this.lighting.brightness;
    u.lightOn.value = Number(this.lighting.on);
    u.falloff.value = Number(this.lighting.falloff);
    this.marker.visible = this.mode !== 'parts' && this.lighting.on;
  }

  arrange() {
    if (!this.data) return;
    let maxX = 0, maxY = 0;
    for (const entry of this.entries) {
      const size = entry.bounds.getSize(new THREE.Vector3());
      maxX = Math.max(maxX, size.x);
      maxY = Math.max(maxY, size.y);
    }
    this.entries.forEach((entry, i) => {
      const {mesh, part, bounds, label} = entry;
      if (this.mode === 'parts') {
        const center = bounds.getCenter(new THREE.Vector3());
        const x = (i % 3 - 1) * (maxX + 23);
        const y = (1 - Math.floor(i / 3)) * (maxY + 32);
        mesh.matrix.makeTranslation(x-center.x, y-center.y, -bounds.min.z);
        entry.labelPosition = new THREE.Vector3(x, y-bounds.getSize(new THREE.Vector3()).y/2-10, 1);
      } else {
        mesh.matrix.fromArray(part.assembly_matrix.flat()).transpose();
        if (this.mode === 'model') {
          const offset = new THREE.Vector3(...part.exploded_offset_mm).multiplyScalar(this.explode);
          mesh.matrix.setPosition(new THREE.Vector3().setFromMatrixPosition(mesh.matrix).add(offset));
        }
      }
      mesh.matrixWorldNeedsUpdate = true;
      mesh.visible = this.mode !== 'model' || part.name !== 'light_hood' || this.showHood;
      label.hidden = this.mode !== 'parts';
    });
    this.group.updateMatrixWorld(true);
  }

  setMode(mode) {
    this.mode = mode;
    const light = mode === 'light';
    this.scene.background.set(light ? '#18201d' : '#eceee7');
    this.grid.visible = !light;
    this.ambient.intensity = light ? .6 : 1.6;
    this.key.intensity = light ? 1 : 3.2;
    this.fill.intensity = light ? .3 : 1.1;
    if (this.plane) this.plane.visible = light;
    this.marker.visible = !!this.data && mode !== 'parts' && this.lighting.on;
    this.arrange();
    this.resetCamera();
  }

  setExplode(value) { this.explode = value; this.arrange(); }
  setHood(value) { this.showHood = value; this.arrange(); }

  frame(bounds, direction, up = new THREE.Vector3(0, 0, 1)) {
    const center = bounds.getCenter(new THREE.Vector3());
    const size = bounds.getSize(new THREE.Vector3());
    const extent = Math.max(size.y, size.x / this.camera.aspect, size.z, 70);
    const distance = extent / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * (this.mode === 'model' ? 1.65 : 1.4);
    this.controls.dispose();
    this.camera.up.copy(up);
    this.camera.position.copy(center).add(direction.normalize().multiplyScalar(distance));
    this.camera.lookAt(center);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.minDistance = 5;
    this.controls.maxDistance = 20000;
    this.controls.target.copy(center);
    this.controls.update();
  }

  resetCamera() {
    if (!this.data) { this.camera.position.set(200, -250, 220); this.camera.lookAt(0, 0, 0); return; }
    if (this.mode === 'light') {
      this.frame(new THREE.Box3().setFromObject(this.plane), new THREE.Vector3(.25, -.4, 1.6), new THREE.Vector3(0, 1, 0));
    } else {
      this.frame(new THREE.Box3().setFromObject(this.group), this.mode === 'parts' ? new THREE.Vector3(.15, -.6, 2.5) : new THREE.Vector3(1.3, -1.6, 1.7));
    }
  }

  wallView() {
    if (this.plane) this.frame(new THREE.Box3().setFromObject(this.plane), new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 1, 0));
  }

  animate() {
    this.animation = requestAnimationFrame(this.animate);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    if (this.mode === 'parts') for (const entry of this.entries) {
      if (!entry.labelPosition) continue;
      const p = entry.labelPosition.clone().project(this.camera);
      entry.label.style.left = `${(p.x+1)*this.container.clientWidth/2}px`;
      entry.label.style.top = `${(1-p.y)*this.container.clientHeight/2}px`;
      entry.label.hidden = p.z > 1;
    }
  }
}
