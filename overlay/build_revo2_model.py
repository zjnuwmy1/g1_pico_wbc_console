#!/usr/bin/env python3
"""Build an overlay G1 MJCF whose Dex3 3-finger hands are replaced by BrainCo
Revo2 6-DOF hands — WITHOUT modifying the read-only gear_sonic upstream.

Inputs (read-only):
  - gear_sonic g1 body MJCF (29-dof body + Dex3 hands)
  - compiled BrainCo Revo2 hand MJCFs (from brainco_{left,right}.urdf)
Output:
  - overlay/g1_revo2/g1_29dof_with_revo2_hand.xml   (self-contained, flat meshdir)
  - overlay/g1_revo2/scene_43dof_revo2.xml           (ROBOT_SCENE target)

Wrist attach (from unitree g1_with_brainco_hand.urdf):
  parent {side}_wrist_yaw_link, xyz=0.0591 0 0, rpy=(±1.5708, 0, ±1.5708)
Mimic (MuJoCo URDF import drops <mimic>): distal = mult * proximal
  thumb mult=1.0 ; index/middle/ring/pinky mult=1.155
"""
import copy
import os
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation

HOME = os.path.expanduser("~")
G1_XML = f"{HOME}/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl/gear_sonic/data/robot_model/model_data/g1/g1_29dof_with_hand.xml"
BRAINCO = {
    "left":  f"{HOME}/Desktop/g1_pico_wbc_console/_spike_brainco_left.xml",
    "right": f"{HOME}/Desktop/g1_pico_wbc_console/_spike_brainco_right.xml",
}
OUT_DIR = f"{HOME}/Desktop/g1_pico_wbc_console/overlay/g1_revo2"
MESHDIR = f"{OUT_DIR}/meshes"  # flat dir with symlinks to g1 body + brainco meshes
OUT_ROBOT = f"{OUT_DIR}/g1_29dof_with_revo2_hand.xml"
OUT_SCENE = f"{OUT_DIR}/scene_43dof_revo2.xml"

FINGERS = ["thumb_metacarpal", "thumb_proximal", "index_proximal",
           "middle_proximal", "ring_proximal", "pinky_proximal"]
MIMIC = [  # (distal_joint, proximal_joint, multiplier)
    ("thumb_distal", "thumb_proximal", 1.0),
    ("index_distal", "index_proximal", 1.155),
    ("middle_distal", "middle_proximal", 1.155),
    ("ring_distal", "ring_proximal", 1.155),
    ("pinky_distal", "pinky_proximal", 1.155),
]
# Finger-root bodies (direct children of worldbody in compiled brainco MJCF)
FINGER_ROOTS = ["{s}_thumb_metacarpal_link", "{s}_index_proximal_link",
                "{s}_middle_proximal_link", "{s}_ring_proximal_link",
                "{s}_pinky_proximal_link"]
# Base(palm) orientation relative to the wrist, as MuJoCo quat (w x y z).
# Derived empirically from the OFFICIAL unitree g1+brainco urdf compiled in
# MuJoCo (ground truth), NOT from naive rpy->quat: the urdf rpy convention did
# not reproduce the official mount (hand ended up rotated ~90deg, fingers down
# instead of forward). See build notes / memory.
WRIST_QUAT = {
    "left":  "0.707107 -0.000845 0.000281 0.707106",
    "right": "0.000845 0.707107 -0.707106 0.000281",
}
WRIST_XYZ = "0.0591 0 0"


def rpy_to_quat_wxyz(rpy):
    q = Rotation.from_euler("xyz", rpy).as_quat()  # urdf rpy == extrinsic xyz ; returns x,y,z,w
    return f"{q[3]:.8f} {q[0]:.8f} {q[1]:.8f} {q[2]:.8f}"


def basename_mesh_files(elem):
    """Strip any dir prefix from every mesh file= so flat MESHDIR resolves."""
    for m in elem.iter("mesh"):
        f = m.get("file")
        if f:
            m.set("file", os.path.basename(f))


def strip_ns(tree):
    for el in tree.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def main():
    g1 = ET.parse(G1_XML)
    groot = g1.getroot()

    # ---- compiler: point at flat overlay meshdir (absolute) ----
    comp = groot.find("compiler")
    comp.set("meshdir", MESHDIR)

    asset = groot.find("asset")
    actuator = groot.find("actuator")
    sensor = groot.find("sensor")
    worldbody = groot.find("worldbody")

    # ---- 1. remove Dex3 hand meshes from <asset> ----
    for m in list(asset.findall("mesh")):
        n = m.get("name", "")
        if "_hand_" in n:  # left_hand_thumb_0_link etc.
            asset.remove(m)

    # ---- 2. remove Dex3 hand actuators & sensors ----
    for a in list(actuator):
        if "_hand_" in (a.get("name", "") + a.get("joint", "")):
            actuator.remove(a)
    if sensor is not None:
        for s in list(sensor):
            txt = s.get("name", "") + s.get("joint", "") + s.get("actuator", "")
            if "_hand_" in txt:
                sensor.remove(s)

    # ---- equality block for mimic ----
    equality = groot.find("equality")
    if equality is None:
        equality = ET.SubElement(groot, "equality")

    # ---- per-side graft ----
    body_by_name = {b.get("name"): b for b in groot.iter("body")}
    for side in ("left", "right"):
        bc = ET.parse(BRAINCO[side]); strip_ns(bc); bcr = bc.getroot()
        bc_asset = bcr.find("asset"); bc_world = bcr.find("worldbody")
        basename_mesh_files(bc_asset)
        # add brainco hand meshes
        for m in bc_asset.findall("mesh"):
            asset.append(copy.deepcopy(m))

        wrist = body_by_name[f"{side}_wrist_yaw_link"]
        # remove ALL Dex3 hand content under wrist: palm geoms + finger bodies
        for child in list(wrist.findall("body")):
            if "_hand_" in child.get("name", ""):
                wrist.remove(child)
        for g in list(wrist.findall("geom")):
            if "_hand_" in (g.get("mesh", "") or ""):
                wrist.remove(g)

        # static flange ring (base2_link) between wrist and hand — present in the
        # unitree g1+brainco urdf (right_base2_joint: parent wrist_yaw, xyz
        # 0.0415 0 0, no rot). The standalone brainco urdf omits it.
        fm = ET.SubElement(asset, "mesh")
        fm.set("name", f"{side}_base2_link")
        fm.set("file", f"{side}_base2_link.STL")
        fg = ET.SubElement(wrist, "geom")
        fg.set("type", "mesh"); fg.set("mesh", f"{side}_base2_link")
        fg.set("pos", "0.0415 0 0"); fg.set("rgba", "0.7 0.7 0.7 1")
        fg.set("contype", "0"); fg.set("conaffinity", "0"); fg.set("group", "1")

        # new brainco base body under wrist
        base = ET.SubElement(wrist, "body")
        base.set("name", f"{side}_revo2_base")
        base.set("pos", WRIST_XYZ)
        base.set("quat", WRIST_QUAT[side])
        # base_link (palm) geoms: COPY them verbatim from the compiled brainco
        # worldbody so their quat/pos (palm orientation) is preserved — building
        # a fresh geom dropped that quat and left the palm misaligned vs fingers.
        for g in bc_world.findall("geom"):
            if "base_link" in (g.get("mesh", "") or "").lower():
                gc = copy.deepcopy(g)
                gc.set("rgba", "0.7 0.7 0.7 1")  # match body grey
                base.append(gc)
        # splice ALL finger-root bodies (5) straight from compiled brainco world
        for fb in bc_world.findall("body"):
            base.append(copy.deepcopy(fb))

        # Drop the 5 mimic DISTAL joints: base_sim maps torques by (joint_index-1)
        # and assumes every non-floating joint is actuated & contiguous. Passive
        # mimic joints inflate njnt and push actuated joint indices past the
        # torque-array size -> out-of-bounds. Removing the <joint> welds each
        # distal link rigidly to its proximal (fingers still curl at proximal).
        for parent in base.iter("body"):
            for j in list(parent.findall("joint")):
                if "distal" in j.get("name", ""):
                    parent.remove(j)
        # rename the 6 actuated joints to contain "_hand_" so base_sim's
        # substring discovery (base_sim.py:237 "left_hand"/"right_hand") finds
        # exactly them.
        actuated_old = {f"{side}_{f}_joint" for f in FINGERS}
        for j in base.iter("joint"):
            if j.get("name") in actuated_old:
                f = j.get("name")[len(side) + 1:-6]  # strip "{side}_" + "_joint"
                j.set("name", f"{side}_hand_{f}_joint")

        # Disable collision on ALL hand geoms (visual-only). The compiled brainco
        # geoms include a collision copy whose thumb_proximal overlaps the palm
        # (base_link) by ~8mm at rest -> a permanent self-contact that destabilises
        # the hand and folds the arm. The sim does no hand manipulation, so the
        # hand never needs collision. (contype/conaffinity = 0 = no contacts.)
        for g in base.iter("geom"):
            g.set("contype", "0")
            g.set("conaffinity", "0")

        # The brainco joints carry NO damping/armature/frictionloss; their links
        # are near-massless (inertia ~1e-7), so any PD torque blows up into a
        # high-freq spasm ("抽搐"). Match the upstream Dex3 finger_motor class
        # (damping 0.05, armature 0.01, frictionloss 0.1) to make them stable.
        for j in base.iter("joint"):
            j.set("damping", "0.05")
            j.set("armature", "0.01")
            j.set("frictionloss", "0.1")

        # ---- actuators: 6 torque motors / hand (renamed joints) ----
        for f in FINGERS:
            mo = ET.SubElement(actuator, "motor")
            mo.set("name", f"{side}_hand_{f}")
            mo.set("joint", f"{side}_hand_{f}_joint")
            mo.set("ctrlrange", "-2 2")
        # (mimic equality intentionally omitted — distal joints were removed and
        #  welded rigid; see distal-removal note above.)

    # CRITICAL: base_sim applies torques via `ctrl[joint_id-1] = torque`, which
    # REQUIRES the <actuator> block to be in the same order as the joint tree
    # (actuator[i] must drive joint i+1). We appended the hand actuators at the
    # end, but the hand joints sit BETWEEN the arms in the tree, so the order
    # was scrambled -> right-arm/hand torques went to the wrong joints and the
    # arm drifted into a contorted pose. Reorder actuators to match joint order.
    joint_order = {j.get("name"): i for i, j in enumerate(worldbody.iter("joint"))}
    acts = list(actuator)
    for a in acts:
        actuator.remove(a)
    acts.sort(key=lambda a: joint_order.get(a.get("joint"), 1 << 30))
    for a in acts:
        actuator.append(a)

    os.makedirs(OUT_DIR, exist_ok=True)
    ET.indent(g1, space="  ")
    g1.write(OUT_ROBOT, encoding="utf-8", xml_declaration=True)

    # scene wrapper: clone the upstream scene (floor / lights / skybox / haze /
    # groundplane material) and only swap the robot <include> to our Revo2 model.
    # Building a bare <include> scene drops all of that -> black, floorless view.
    SCENE_SRC = os.path.dirname(G1_XML) + "/scene_43dof.xml"
    scene = ET.parse(SCENE_SRC)
    sroot = scene.getroot()
    sroot.set("model", "g1_revo2_scene")
    for inc in sroot.findall("include"):
        inc.set("file", os.path.basename(OUT_ROBOT))
    scene.write(OUT_SCENE, encoding="utf-8", xml_declaration=True)
    print("WROTE", OUT_ROBOT)
    print("WROTE", OUT_SCENE)


if __name__ == "__main__":
    main()
