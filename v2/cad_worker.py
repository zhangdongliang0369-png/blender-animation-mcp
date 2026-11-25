"""Isolated Open CASCADE STEP-to-GLB conversion preserving XCAF assembly labels."""
import json
from pathlib import Path

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_RetDone
from OCP.Message import Message_ProgressRange
from OCP.RWGltf import RWGltf_CafWriter
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TColStd import TColStd_IndexedDataMapOfStringString
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDF import TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool


def main():
    cfg = json.loads(Path("/inputs/job.json").read_text())
    source = Path("/inputs") / cfg["model"]
    document = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    if reader.ReadFile(str(source)) != IFSelect_RetDone:
        raise ValueError("Open CASCADE could not read the STEP file")
    if not reader.Transfer(document):
        raise ValueError("Open CASCADE could not transfer the STEP assembly")
    # STEPCAF transfers geometry to the OCCT internal unit, millimetres by default.
    XCAFDoc_DocumentTool.SetLengthUnit_s(document, 0.001)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    if roots.Length() == 0:
        raise ValueError("The STEP file contains no transferable shapes")
    for index in range(1, roots.Length() + 1):
        shape = tool.GetShape_s(roots.Value(index))
        BRepMesh_IncrementalMesh(shape, 0.15, False, 0.35, False).Perform()
    writer = RWGltf_CafWriter(TCollection_AsciiString("/output/converted.glb"), True)
    if not writer.Perform(document, TColStd_IndexedDataMapOfStringString(), Message_ProgressRange()):
        raise ValueError("Open CASCADE could not write the tessellated GLB")


if __name__ == "__main__":
    main()
