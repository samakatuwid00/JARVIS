$code = @'
using System.Runtime.InteropServices;
[Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ISimpleAudioVolume { void SetMasterVolume(float fLevel,[MarshalAs(UnmanagedType.LPStruct)] System.Guid refEventContext); float GetMasterVolume(); }
[Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator { void _S1(); void _S2(); IMMDevice GetDefaultAudioEndpoint(int dataFlow, int role); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice { void _S1(); void _S2(); void _S3(); void _S4(); void _S5(); void _S6(); ISimpleAudioVolume Activate([MarshalAs(UnmanagedType.LPStruct)] System.Guid iid, int dwClsCtx, int pActivationParams); }
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")] public class MMDeviceEnumerator {}
'@
Add-Type -TypeDefinition $code
$e = [System.Activator]::CreateInstance([MMDeviceEnumerator])
$de = $e -as [IMMDeviceEnumerator]
$d = $de.GetDefaultAudioEndpoint(0,1)
$sv = $d.Activate([System.Guid]"87CE5498-68D6-44E5-9215-6DA47EF883D8", 1, $null)
$sv.SetMasterVolume(0.8,[System.Guid]::Empty)
Write-Host ("volume set: {0:P0}" -f $sv.GetMasterVolume())
