# Unreal Engine 5.8 Integration Guide

> **Important Environment Note:** Unreal Engine 5.8 and Cosys-AirSim are NOT installed on the integration development machine. The C++ adapter design and Blueprint/Actor instructions below were authored against Unreal Engine 5.8's `Http` (`FHttpModule`) and `Json` (`FJsonObject`) modules and must be compiled and verified on the team member's Unreal Engine 5.8 workstation.

---

## 1. Architecture Summary

Do **not** port `liboqs`, `cryptography`, or the Python RL/Intent engines into Unreal C++. Instead:

1. Run the Python Three-Layer C2 Security Server (`python integration_entry_point.py --mode server`).
2. Inside Unreal Engine 5.8, add a C++ ActorComponent (`UUavSecurityClientComponent`) or an Unreal Plugin module (`UavC2SecurityBridge`) that:
   - Submits pilot / GCS commands to `POST /api/v1/command` (or via TCP port `8766`).
   - Polls `GET /api/v1/commands/pending` and executes **only** commands returned from the verified queue (`accepted == true`).
   - Sends `POST /api/v1/commands/ack` after dispatching each command to the pawn / AirSim controller.
   - Reports Unreal pawn position and touchdown back via `POST /api/v1/telemetry/update`.

---

## 2. Step-by-Step UE 5.8 Project Setup

### Step 2.1 Enable `HTTP` and `Json` Modules in `Build.cs`
Open `<YourProject>/Source/<YourProject>/<YourProject>.Build.cs` and ensure `HTTP`, `Json`, `JsonUtilities`, and `Sockets` are in `PublicDependencyModuleNames`:

```csharp
PublicDependencyModuleNames.AddRange(new string[] {
    "Core",
    "CoreUObject",
    "Engine",
    "InputCore",
    "HTTP",
    "Json",
    "JsonUtilities",
    "Sockets",
    "Networking"
});
```

### Step 2.2 Optional: Packaging as a Reusable UE 5.8 Plugin
To create a standalone Unreal plugin:
1. In UE 5.8 Editor: **Edit -> Plugins -> Add -> Blank** named `UavC2SecurityBridge`.
2. Add `"HTTP"`, `"Json"`, `"JsonUtilities"` to `Plugins/UavC2SecurityBridge/Source/UavC2SecurityBridge/UavC2SecurityBridge.Build.cs`.
3. Place `UavSecurityClientComponent.h` and `UavSecurityClientComponent.cpp` below into the plugin's `Public/` and `Private/` folders.
4. Regenerate Visual Studio project files and build in **Development Editor (Win64)**.

---

## 3. Reference UE 5.8 C++ Component (`UUavSecurityClientComponent`)

### `UavSecurityClientComponent.h`
```cpp
#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "Interfaces/IHttpRequest.h"
#include "UavSecurityClientComponent.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_FourParams(
    FOnApprovedUavCommand,
    FString, CommandId,
    FString, CommandType,
    FVector, TargetLocationMeters,
    float, SpeedMps
);

DECLARE_DYNAMIC_MULTICAST_DELEGATE_ThreeParams(
    FOnSecurityRejection,
    FString, FailedLayer,
    FString, ErrorCode,
    FString, Reason
);

UCLASS(ClassGroup=(Custom), meta=(BlueprintSpawnableComponent))
class YOURPROJECT_API UUavSecurityClientComponent : public UActorComponent
{
    GENERATED_BODY()

public:
    UUavSecurityClientComponent();

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="UAV C2 Security")
    FString SecurityServerBaseUrl = TEXT("http://127.0.0.1:8765/api/v1");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="UAV C2 Security")
    FString ApiKey = TEXT("");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="UAV C2 Security")
    float PollIntervalSeconds = 0.25f;

    UPROPERTY(BlueprintAssignable, Category="UAV C2 Security")
    FOnApprovedUavCommand OnApprovedCommand;

    UPROPERTY(BlueprintAssignable, Category="UAV C2 Security")
    FOnSecurityRejection OnSecurityRejection;

    UFUNCTION(BlueprintCallable, Category="UAV C2 Security")
    void SubmitCommand(const FString& CommandType, const FString& Waypoint, float X, float Y, float Altitude, float Speed);

    UFUNCTION(BlueprintCallable, Category="UAV C2 Security")
    void PollPendingCommands();

    UFUNCTION(BlueprintCallable, Category="UAV C2 Security")
    void PushTelemetry(float X, float Y, float Altitude, float Speed, bool bInAir, bool bLanded);

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

private:
    FTimerHandle PollTimerHandle;
    void AckCommand(const FString& CommandId);
    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> CreateJsonRequest(const FString& Endpoint, const FString& Verb);
};
```

### `UavSecurityClientComponent.cpp`
```cpp
#include "UavSecurityClientComponent.h"
#include "HttpModule.h"
#include "Interfaces/IHttpResponse.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "TimerManager.h"

UUavSecurityClientComponent::UUavSecurityClientComponent()
{
    PrimaryComponentTick.bCanEverTick = false;
}

void UUavSecurityClientComponent::BeginPlay()
{
    Super::BeginPlay();
    if (GetWorld())
    {
        GetWorld()->GetTimerManager().SetTimer(
            PollTimerHandle,
            this,
            &UUavSecurityClientComponent::PollPendingCommands,
            PollIntervalSeconds,
            true
        );
    }
}

void UUavSecurityClientComponent::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    if (GetWorld())
    {
        GetWorld()->GetTimerManager().ClearTimer(PollTimerHandle);
    }
    Super::EndPlay(EndPlayReason);
}

TSharedRef<IHttpRequest, ESPMode::ThreadSafe> UUavSecurityClientComponent::CreateJsonRequest(
    const FString& Endpoint, const FString& Verb)
{
    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = FHttpModule::Get().CreateRequest();
    Req->SetURL(SecurityServerBaseUrl + Endpoint);
    Req->SetVerb(Verb);
    Req->SetHeader(TEXT("Content-Type"), TEXT("application/json"));
    Req->SetTimeout(5.0f);
    if (!ApiKey.IsEmpty())
    {
        Req->SetHeader(TEXT("X-API-Key"), ApiKey);
    }
    return Req;
}

void UUavSecurityClientComponent::SubmitCommand(
    const FString& CommandType, const FString& Waypoint, float X, float Y, float Altitude, float Speed)
{
    TSharedPtr<FJsonObject> ParamsObj = MakeShared<FJsonObject>();
    if (!Waypoint.IsEmpty()) ParamsObj->SetStringField(TEXT("waypoint"), Waypoint);
    if (CommandType == TEXT("GOTO"))
    {
        ParamsObj->SetNumberField(TEXT("x"), X);
        ParamsObj->SetNumberField(TEXT("y"), Y);
        ParamsObj->SetNumberField(TEXT("altitude"), Altitude);
        ParamsObj->SetNumberField(TEXT("speed"), Speed);
    }
    else if (CommandType == TEXT("TAKEOFF"))
    {
        ParamsObj->SetNumberField(TEXT("altitude"), Altitude);
    }

    TSharedPtr<FJsonObject> RootObj = MakeShared<FJsonObject>();
    RootObj->SetStringField(TEXT("command_type"), CommandType);
    RootObj->SetObjectField(TEXT("parameters"), ParamsObj);

    FString Body;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Body);
    FJsonSerializer::Serialize(RootObj.ToSharedRef(), Writer);

    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = CreateJsonRequest(TEXT("/command"), TEXT("POST"));
    Req->SetContentAsString(Body);
    Req->OnProcessRequestComplete().BindLambda(
        [this](FHttpRequestPtr, FHttpResponsePtr Resp, bool bSucceeded)
        {
            if (!bSucceeded || !Resp.IsValid())
            {
                OnSecurityRejection.Broadcast(TEXT("api"), TEXT("API_TIMEOUT"), TEXT("Security server unreachable"));
                return;
            }
            TSharedPtr<FJsonObject> Json;
            TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Resp->GetContentAsString());
            if (FJsonSerializer::Deserialize(Reader, Json) && Json.IsValid())
            {
                if (!Json->GetBoolField(TEXT("accepted")))
                {
                    FString Layer = Json->HasField(TEXT("failed_layer")) ? Json->GetStringField(TEXT("failed_layer")) : TEXT("unknown");
                    FString Code = Json->GetStringField(TEXT("error_code"));
                    FString Reason = Json->GetStringField(TEXT("reason"));
                    OnSecurityRejection.Broadcast(Layer, Code, Reason);
                }
            }
        });
    Req->ProcessRequest();
}

void UUavSecurityClientComponent::PollPendingCommands()
{
    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = CreateJsonRequest(TEXT("/commands/pending"), TEXT("GET"));
    Req->OnProcessRequestComplete().BindLambda(
        [this](FHttpRequestPtr, FHttpResponsePtr Resp, bool bSucceeded)
        {
            if (!bSucceeded || !Resp.IsValid() || Resp->GetResponseCode() != 200) return;

            TSharedPtr<FJsonObject> Json;
            TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Resp->GetContentAsString());
            if (!FJsonSerializer::Deserialize(Reader, Json) || !Json.IsValid()) return;

            const TArray<TSharedPtr<FJsonValue>>* CommandsArr = nullptr;
            if (!Json->TryGetArrayField(TEXT("commands"), CommandsArr)) return;

            for (const TSharedPtr<FJsonValue>& Val : *CommandsArr)
            {
                TSharedPtr<FJsonObject> CmdObj = Val->AsObject();
                if (!CmdObj.IsValid()) continue;

                FString CmdId = CmdObj->GetStringField(TEXT("command_id"));
                FString CmdType = CmdObj->GetStringField(TEXT("command_type"));
                TSharedPtr<FJsonObject> Params = CmdObj->GetObjectField(TEXT("parameters"));

                float X = Params->HasField(TEXT("x")) ? Params->GetNumberField(TEXT("x")) : 0.0f;
                float Y = Params->HasField(TEXT("y")) ? Params->GetNumberField(TEXT("y")) : 0.0f;
                float Alt = Params->HasField(TEXT("altitude")) ? Params->GetNumberField(TEXT("altitude")) : 0.0f;
                float Spd = Params->HasField(TEXT("speed")) ? Params->GetNumberField(TEXT("speed")) : 10.0f;

                OnApprovedCommand.Broadcast(CmdId, CmdType, FVector(X, Y, Alt), Spd);
                AckCommand(CmdId);
            }
        });
    Req->ProcessRequest();
}

void UUavSecurityClientComponent::AckCommand(const FString& CommandId)
{
    TSharedPtr<FJsonObject> Obj = MakeShared<FJsonObject>();
    Obj->SetStringField(TEXT("command_id"), CommandId);
    FString Body;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Body);
    FJsonSerializer::Serialize(Obj.ToSharedRef(), Writer);

    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = CreateJsonRequest(TEXT("/commands/ack"), TEXT("POST"));
    Req->SetContentAsString(Body);
    Req->ProcessRequest();
}

void UUavSecurityClientComponent::PushTelemetry(
    float X, float Y, float Altitude, float Speed, bool bInAir, bool bLanded)
{
    TSharedPtr<FJsonObject> Obj = MakeShared<FJsonObject>();
    Obj->SetNumberField(TEXT("x"), X);
    Obj->SetNumberField(TEXT("y"), Y);
    Obj->SetNumberField(TEXT("z_altitude"), Altitude);
    Obj->SetNumberField(TEXT("speed"), Speed);
    Obj->SetBoolField(TEXT("in_air"), bInAir);
    Obj->SetBoolField(TEXT("armed"), bInAir);
    Obj->SetBoolField(TEXT("landed"), bLanded);

    FString Body;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Body);
    FJsonSerializer::Serialize(Obj.ToSharedRef(), Writer);

    TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = CreateJsonRequest(TEXT("/telemetry/update"), TEXT("POST"));
    Req->SetContentAsString(Body);
    Req->ProcessRequest();
}
```

---

## 4. Network & Coordinate Notes for UE 5.8

1. **Units**: Layer 3 mission limits (`x`, `y`, `altitude`, `speed`) are in **meters** and **m/s**. Unreal Engine world coordinates are in **centimeters** (`1 m = 100 UU`). Scale `(X, Y, Z)` by `100.0f` when converting to `FVector` for Actor placement (`UE_Z = +Altitude * 100.0f`), or use Cosys-AirSim's NED meter coordinates directly (see `COSYS_AIRSIM_SETUP.md`).
2. **Remote Workstation Binding**: If the security server runs on Machine A and UE 5.8 runs on Machine B on the same LAN, start the server with `UAV_C2_HTTP_HOST=0.0.0.0`, set `UAV_C2_API_KEY=<strong-random-key>`, and point `SecurityServerBaseUrl` to `http://<Machine-A-LAN-IP>:8765/api/v1`.
3. **Fail-Safe Policy**: Never bind UE input directly to drone movement without routing through `SubmitCommand` -> `PollPendingCommands`.
